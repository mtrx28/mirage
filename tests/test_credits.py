"""The credit ledger and the per-call meter."""
from __future__ import annotations

import json
from dataclasses import replace

from mirage.config import VoiceConfig
from mirage.credits import CallMeter, Ledger, main as credits_main


def cfg_for(tmp_path, **overrides) -> VoiceConfig:
    return replace(VoiceConfig(), ledger_path=tmp_path / "credits.json", **overrides)


def test_a_new_ledger_starts_from_the_dashboard_seed(tmp_path):
    ledger = Ledger(cfg_for(tmp_path))
    assert ledger.spent == 18.37 and ledger.requests == 23
    assert json.loads((tmp_path / "credits.json").read_text())["spent"] == 18.37


def test_the_total_survives_a_restart(tmp_path):
    cfg = cfg_for(tmp_path)
    Ledger(cfg).add(1.5, requests=1)
    again = Ledger(cfg)
    assert abs(again.spent - 19.87) < 1e-9 and again.requests == 24


def test_hard_stop_is_80_percent_of_the_pool(tmp_path):
    ledger = Ledger(cfg_for(tmp_path))
    assert ledger.limit == 4000.0 and ledger.warn_at == 3500.0
    ledger.sync(3999.99)
    assert not ledger.blocked() and ledger.blocked(extra=0.02)
    ledger.sync(4000.0)
    assert ledger.blocked()
    assert ledger.warning()


def test_sync_replaces_the_total_and_keeps_requests_unless_given(tmp_path):
    ledger = Ledger(cfg_for(tmp_path))
    ledger.add(5, requests=2)
    ledger.sync(30.0)
    assert ledger.spent == 30.0 and ledger.requests == 25
    ledger.sync(31.0, requests=40)
    assert Ledger(cfg_for(tmp_path)).requests == 40


def test_a_corrupt_ledger_blocks_spending_until_synced(tmp_path):
    cfg = cfg_for(tmp_path)
    (tmp_path / "credits.json").write_text("{not json")
    ledger = Ledger(cfg)
    assert ledger.blocked()                                       # the real total is unknown
    assert (tmp_path / "credits.json.corrupt").exists()           # kept for inspection
    ledger.sync(20.0)
    assert not Ledger(cfg).blocked()


def test_the_sync_command(tmp_path, monkeypatch, capsys):
    cfg = cfg_for(tmp_path)
    monkeypatch.setattr("mirage.credits.VoiceConfig", lambda: cfg)
    credits_main(["sync", "25.5", "--requests", "30"])
    out = capsys.readouterr().out
    assert "25.50" in out and "30 requests" in out
    assert Ledger(cfg).spent == 25.5


# ------------------------------------------------------------------ the per-call meter
def test_speech_to_text_uses_the_larger_of_two_counts(tmp_path):
    meter = CallMeter(cfg_for(tmp_path), Ledger(cfg_for(tmp_path)))
    meter.voiced_s, meter.reported_s = 60.0, 30.0
    assert abs(meter.stt_credits - 0.45) < 1e-9
    meter.reported_s = 120.0
    assert abs(meter.stt_credits - 0.9) < 1e-9


def test_text_to_speech_is_counted_per_character(tmp_path):
    meter = CallMeter(cfg_for(tmp_path), Ledger(cfg_for(tmp_path)))
    meter.tts_chars = 1000
    assert abs(meter.tts_credits - 2.7) < 1e-9
    meter.voiced_s = 60
    assert abs(meter.credits - 3.15) < 1e-9


def test_flushing_adds_only_the_new_part_and_counts_one_request(tmp_path):
    cfg = cfg_for(tmp_path)
    ledger = Ledger(cfg)
    meter = CallMeter(cfg, ledger)
    meter.voiced_s = 60
    meter.flush(force=True)
    meter.voiced_s = 120
    meter.flush(force=True)
    assert abs(ledger.spent - (18.37 + 0.9)) < 1e-9
    assert ledger.requests == 24
    assert Ledger(cfg).spent == ledger.spent                      # and it is on disk


def test_a_throttled_flush_does_nothing_until_due(tmp_path):
    cfg = cfg_for(tmp_path)
    ledger = Ledger(cfg)
    meter = CallMeter(cfg, ledger)
    meter.voiced_s = 600
    meter.flush()                                                 # just created: not due yet
    assert ledger.spent == 18.37


def test_the_call_stops_when_this_call_would_cross_the_pool_limit(tmp_path):
    cfg = cfg_for(tmp_path)
    ledger = Ledger(cfg)
    ledger.sync(3999.0)
    meter = CallMeter(cfg, ledger)
    assert not meter.over_pool_limit()
    meter.voiced_s = 60 * 3                                       # 1.35 credits not yet in the ledger
    assert meter.over_pool_limit()
    meter.flush(force=True)                                       # now in the ledger; still over the limit
    assert meter.over_pool_limit()


def test_per_call_cap(tmp_path):
    meter = CallMeter(cfg_for(tmp_path, per_call_cap_credits=1.0), Ledger(cfg_for(tmp_path)))
    meter.tts_chars = 300
    assert not meter.over_call_cap()
    meter.tts_chars = 400
    assert meter.over_call_cap()
