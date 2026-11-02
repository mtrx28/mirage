"""Stop 3: the full loop (LiveCall), against a scripted Prisma, a fake Timbre and a pretend page.

No test touches the real Gnani service. Timings are shrunk (waits of tens of milliseconds) so each test runs in about a second.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from mirage.config import FSMConfig, VoiceConfig
from mirage.credits import Ledger
from mirage.fsm import CallBrain
from mirage.gaterequired import GateRequired
from mirage.live import LiveCall
from mirage.safety import Gate
from mirage.timbre import AudioCache, TimbreUnavailable

KEY = "SECRETKEY123"
PHONE = "0000274196"


def pcm_bytes(seconds: float = 0.2, rate: int = 24000) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * math.pi * 300 * t) * 0.3 * 32767).astype("<i2").tobytes()


def mic_chunk(seconds: float = 0.02, rate: int = 48000, start: float = 0.0) -> bytes:
    t = start + np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * math.pi * 440 * t) * 0.5 * 32767).astype("<i2").tobytes()


class ScriptLink:
    """Stands in for Prisma: records the frames sent to it and lets the test say what Prisma 'heard'."""
    instances: list = []

    def __init__(self, cfg, key, on_message, on_closed, keyterms=None):
        self.on_message, self.on_closed, self.frames = on_message, on_closed, []
        self.request_ids, self.reconnects, self.frames_lost = ["script"], 0, 0
        self.sent_header_names, self.keyterms_header_chars = [], 0
        ScriptLink.instances.append(self)

    async def open(self):
        await self.on_message({"type": "connected", "request_id": "script"})

    async def send_frame(self, frame):
        self.frames.append(frame)

    async def close(self):
        pass


class FakeTTS:
    def __init__(self, seconds=0.2, delay_first=0.0, fail=None, hang=False):
        self.texts, self.pcm, self.delay_first, self.fail, self.hang = [], pcm_bytes(seconds), delay_first, fail, hang

    async def synthesize(self, text, on_chunk, on_first_audio=None):
        self.texts.append(text)
        if self.fail:
            raise self.fail
        if self.hang:
            await asyncio.sleep(30)
        await asyncio.sleep(self.delay_first)
        half = len(self.pcm) // 4 * 2
        await on_chunk(self.pcm[:half])
        if on_first_audio:
            on_first_audio()
        await on_chunk(self.pcm[half:])
        return self.pcm, 0.05, 1

    async def aclose(self):
        pass


def fsm_cfg(**kw) -> FSMConfig:
    base = dict(synthetic_mode=True, normal_wait_s=0.05, digit_wait_s=0.4, hold_ceiling_s=2.0, hold_total_cap_s=3.0,
                hold_total_cap_digit_s=6.0)
    base.update(kw)
    return FSMConfig(**base)


class Rig:
    def __init__(self, tmp_path: Path, *, tts=None, autopage=True, fsm=None, gate=None, ledger_spent=None, max_call_s=None, **cfg_over):
        ScriptLink.instances.clear()
        over = dict(tick_s=0.05, unmute_holdoff_s=0.05, playback_margin_s=0.4, goodbye_deadline_s=3.0, mute_grace_s=0.05,
                    capture_stall_min_s=0.05)
        over.update(cfg_over)
        self.cfg = replace(VoiceConfig(), calls_dir=tmp_path / "calls", audio_dir=tmp_path / "audio", ledger_path=tmp_path / "credits.json",
                           lock_path=tmp_path / "lock", tts_first_429_path=tmp_path / "f429", tts_log_path=tmp_path / "tts.jsonl", **over)
        self.ledger = Ledger(self.cfg)
        if ledger_spent is not None:
            self.ledger.sync(ledger_spent)
        self.tts = tts or FakeTTS()
        self.notes: list[dict] = []
        self.audio: list[bytes] = []
        self.autopage = autopage
        self.call = LiveCall(self.cfg, gate or Gate(), self.ledger, self._notify, key=KEY, send_audio=self._send_audio,
                             tts_client=self.tts, cache=AudioCache(self.cfg), fsm_cfg=fsm or fsm_cfg(), link_factory=ScriptLink,
                             max_call_s=max_call_s)
        self.feeder: asyncio.Task | None = None

    async def _notify(self, m):
        self.notes.append(m)
        if self.autopage and m["type"] == "uncle_speech_end":
            async def ack():                                   # a page that plays the audio in real time, then says it is done
                await asyncio.sleep(0.03)
                await self.call.on_page_message({"type": "playing", "output_latency_ms": 20})
                await asyncio.sleep(m["audio_s"] + 0.05)
                await self.call.on_page_message({"type": "playback_done", "underruns": 0, "audio_state": "running"})
            asyncio.create_task(ack())

    async def _send_audio(self, piece):
        self.audio.append(piece)

    async def start(self, mic: bool = True):
        await self.call.start(48000)
        self.link = ScriptLink.instances[-1]
        if mic:
            self.feeder = asyncio.create_task(self._feed())

    async def _feed(self):
        position = 0.0
        while True:
            self.call.feed(mic_chunk(start=position))
            position += 0.02
            await asyncio.sleep(0.02)

    async def say(self, uid: str, text: str, *, audio_s: float = 1.0, start: bool = True):
        if start:
            await self.link.on_message({"type": "speech_start", "utterance_id": uid})
        await self.link.on_message({"type": "transcript", "utterance_id": uid, "text": text, "audio_duration_ms": int(audio_s * 1000),
                                    "latency": 200, "is_final": True})

    def kinds(self) -> list[str]:
        return [n["type"] for n in self.notes]

    def turns(self) -> list[dict]:
        return [n for n in self.notes if n["type"] == "turn"]

    async def until(self, predicate, timeout: float = 5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"timed out; notes so far: {self.kinds()}")

    async def close(self):
        if self.feeder:
            self.feeder.cancel()
        await self.call.hangup()

    def folder(self) -> Path:
        return self.cfg.calls_dir / self.call.log.call_id

    def rows(self, name: str) -> list[dict]:
        path = self.folder() / name
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def run(coro):
    return asyncio.run(coro)


def voiced(frame: bytes) -> bool:
    return frame != bytes(len(frame))


# ------------------------------------------------------------------ a normal turn, the mute, playback done
def test_a_turn_runs_brain_gate_tts_and_page_with_the_mute_on_only_while_uncle_speaks(tmp_path):
    async def go():
        rig = Rig(tmp_path)
        await rig.start()
        await rig.say("u1", "namaste main Rajesh bol raha hoon")
        await rig.until(lambda: "mic" in rig.kinds())
        mute_at = len(rig.link.frames)
        await rig.until(lambda: any(n["type"] == "mic" and not n["muted"] for n in rig.notes))
        unmute_at = len(rig.link.frames)
        await asyncio.sleep(0.3)
        after = rig.link.frames[unmute_at + 2:]
        before = rig.link.frames[:mute_at - 3]
        await rig.close()
        return rig, mute_at, unmute_at, before, after

    rig, mute_at, unmute_at, before, after = run(go())
    turn = rig.turns()[0]
    assert turn["uncle_text"] and turn["tactic"] and turn["stage"] and turn["intel"] == [] and "wait" in turn
    assert rig.tts.texts == [turn["uncle_text"]] and b"".join(rig.audio) == rig.tts.pcm      # the gated line, exactly, and all its audio
    muted = rig.link.frames[mute_at + 1:unmute_at - 1]
    assert muted and not any(voiced(f) for f in muted)                                      # silence reached Prisma while Uncle spoke
    assert any(voiced(f) for f in before) and any(voiced(f) for f in after)                 # the microphone was live before and after
    assert all(len(f) == 1024 for f in rig.link.frames)
    kinds = rig.kinds()
    assert kinds.index("uncle_speech_start") < kinds.index("uncle_speech_end") < kinds.index("latency")
    summary = json.loads((rig.folder() / "summary.json").read_text(encoding="utf-8"))
    assert summary["audio"]["muted_frames"] > 0 and summary["uncle_lines_spoken"] == 1 and summary["unspoken_turns"] == 0


def test_every_turn_leaves_a_latency_row_with_the_wait_as_its_own_column(tmp_path):
    async def go():
        rig = Rig(tmp_path)
        await rig.start()
        await rig.say("u1", "namaste main Rajesh bol raha hoon")
        await rig.until(lambda: "latency" in rig.kinds() and rig.rows("turns.jsonl") == [] or "latency" in rig.kinds())
        await rig.close()
        return rig

    rig = run(go())
    row = rig.rows("latency.jsonl")[0]
    for column in ("wait_s", "base_wait_s", "hold_extra_s", "digit_mode", "brain_ms", "gate_ms", "tts_first_byte_ms", "tts_total_ms",
                   "first_audio_sent_s", "page_playing_s", "arrival_to_audible_s", "end_to_audible_est_s", "cached", "chars", "underruns"):
        assert column in row
    assert row["wait_s"] >= 0.05 and row["base_wait_s"] == 0.05 and row["digit_mode"] is False
    assert row["arrival_to_audible_s"] > row["wait_s"] and row["tts_first_byte_ms"] == 50
    turns = rig.rows("turns.jsonl")[0]
    assert turns["spoken"] is True and turns["brain_believes_spoken"] is True and turns["gated"] is True


# ------------------------------------------------------------------ the hold, in the live loop
def test_no_reply_while_an_utterance_is_open_then_one_reply_covering_both(tmp_path):
    async def go():
        rig = Rig(tmp_path)
        await rig.start()
        await rig.say("u1", "namaste main CBI se bol raha hoon")
        await rig.link.on_message({"type": "speech_start", "utterance_id": "u2"})          # the caller carries on
        await asyncio.sleep(0.4)
        quiet = len(rig.turns())
        await rig.say("u2", "aapka account block ho jayega", start=False)
        await rig.until(lambda: rig.turns())
        await rig.close()
        return rig, quiet

    rig, quiet = run(go())
    assert quiet == 0 and len(rig.turns()) == 1 and len(rig.turns()[0]["heard"].split("CBI")) == 2
    assert "account block" in rig.turns()[0]["heard"] and "CBI" in rig.turns()[0]["heard"]


def test_an_empty_transcript_clears_the_hold(tmp_path):
    async def go():
        rig = Rig(tmp_path)
        await rig.start()
        await rig.say("u1", "namaste main CBI se bol raha hoon")
        await rig.link.on_message({"type": "speech_start", "utterance_id": "blip"})
        await asyncio.sleep(0.2)
        assert not rig.turns()
        await rig.link.on_message({"type": "transcript", "utterance_id": "blip", "text": "", "audio_duration_ms": 300, "latency": 9})
        await rig.until(lambda: rig.turns())
        await rig.close()
        return rig

    assert len(run(go()).turns()) == 1


def test_a_stuck_utterance_stops_blocking_at_the_ceiling_and_it_is_logged(tmp_path):
    async def go():
        rig = Rig(tmp_path, fsm=fsm_cfg(hold_ceiling_s=0.5))
        await rig.start()
        await rig.say("u1", "namaste main CBI se bol raha hoon")
        await rig.link.on_message({"type": "speech_start", "utterance_id": "stuck"})       # never finishes
        t0 = time.monotonic()
        await rig.until(lambda: rig.turns())
        await rig.close()
        return rig, time.monotonic() - t0

    rig, waited = run(go())
    assert 0.4 < waited < 1.5
    assert [e["event"] for e in rig.rows("turns.jsonl")[0]["scheduler_events"]] == ["hold_ceiling"]


def test_a_slowly_dictated_number_split_across_utterances_is_answered_once_and_whole(tmp_path):
    async def go():
        rig = Rig(tmp_path)
        await rig.start()
        await rig.say("u1", "main CBI se bol raha hoon phone number hai zero zero")        # ends in digit words: digit mode
        await asyncio.sleep(0.1)
        await rig.link.on_message({"type": "speech_start", "utterance_id": "u2"})
        await asyncio.sleep(0.6)
        quiet = len(rig.turns())
        await rig.say("u2", "zero zero do saat chaar ek nau chhe", start=False)
        await rig.until(lambda: rig.turns())
        await rig.close()
        return rig, quiet

    rig, quiet = run(go())
    assert quiet == 0 and len(rig.turns()) == 1
    assert [f["value"] for f in rig.turns()[0]["intel"] if f["kind"] == "phone"] == [PHONE]
    assert rig.turns()[0]["wait"]["digit_mode"] is True


# ------------------------------------------------------------------ talking over Uncle, and checks that say so plainly
def test_speech_heard_while_uncle_speaks_is_held_and_answered_afterwards_and_new_speech_is_flagged(tmp_path):
    async def go():
        rig = Rig(tmp_path, tts=FakeTTS(seconds=0.6, delay_first=0.3))
        await rig.start()
        await rig.say("u1", "namaste main Rajesh bol raha hoon")
        await rig.until(lambda: rig.turns())                                               # Uncle has decided; his voice is being made
        await rig.link.on_message({"type": "speech_start", "utterance_id": "early"})       # open BEFORE the mute starts
        await rig.until(lambda: "mic" in rig.kinds())
        await asyncio.sleep(0.2)                                                           # well inside the mute window
        await rig.say("early", "suniye suniye zara", audio_s=0.5, start=False)             # its (truncated) transcript: expected
        await rig.say("intruder", "ek minute meri baat suniye", audio_s=0.5)               # NEW speech while muted: an anomaly
        await rig.until(lambda: len(rig.turns()) == 2)
        await rig.close()
        return rig

    rig = run(go())
    assert rig.call.mute_anomalies == 2                           # the intruder's start and its text; the early utterance is NOT one
    assert sum(1 for n in rig.notes if n["type"] == "notice" and "while Uncle's line was playing" in n["message"]) == 1   # said once
    second = rig.turns()[1]
    assert "suniye suniye" in second["heard"] and "ek minute" in second["heard"]            # both answered after Uncle finished
    events = [e["event"] for e in rig.rows("events.jsonl")]
    assert "mute_anomaly" in events and "uncle_speaking_end" in events


def test_a_transcript_that_arrives_while_uncles_voice_is_being_made_waits_until_his_line_is_finished(tmp_path):
    """Two replies must never run at once: the second turn starts only after the first line has played and the mic is live again."""
    async def go():
        rig = Rig(tmp_path, tts=FakeTTS(seconds=0.3, delay_first=0.4))
        await rig.start()
        await rig.say("u1", "namaste main Rajesh bol raha hoon")
        await rig.until(lambda: rig.turns())                                  # decided; the voice is still being made (0.4 s)
        await rig.say("u2", "aapka account block ho jayega")                    # the caller speaks again right now
        await rig.until(lambda: len(rig.turns()) == 2, timeout=8)
        await rig.close()
        return rig

    rig = run(go())
    kinds = [(n["type"], n.get("muted")) for n in rig.notes]
    first_unmuted = kinds.index(("mic", False))
    second_turn = [i for i, n in enumerate(rig.notes) if n["type"] == "turn"][1]
    assert second_turn > first_unmuted                                         # not before the first line finished
    assert len(rig.tts.texts) == 2 and rig.turns()[1]["heard"] == "aapka account block ho jayega"


def test_when_the_page_never_says_playback_is_done_the_mute_ends_by_timer_and_it_says_so(tmp_path):
    async def go():
        rig = Rig(tmp_path, autopage=False, tts=FakeTTS(seconds=0.1))
        await rig.start()
        await rig.say("u1", "namaste main Rajesh bol raha hoon")
        t0 = time.monotonic()
        await rig.until(lambda: any(n["type"] == "mic" and not n["muted"] for n in rig.notes), timeout=6)
        waited = time.monotonic() - t0
        await rig.close()
        return rig, waited

    rig, waited = run(go())
    assert 0.9 < waited < 3 and rig.call.playback_missing == 1
    assert any(n["type"] == "notice" and "did not report" in n["message"] for n in rig.notes)
    assert "playback_done_missing" in [e["event"] for e in rig.rows("events.jsonl")]


def test_if_the_page_stops_sending_microphone_audio_while_uncle_speaks_the_call_says_so(tmp_path):
    async def go():
        rig = Rig(tmp_path, tts=FakeTTS(seconds=0.5), capture_stall_min_s=0.2)
        await rig.start(mic=False)                                    # the page sends NO microphone audio at all
        await rig.say("u1", "namaste main Rajesh bol raha hoon")
        await rig.until(lambda: "latency" in rig.kinds())
        await rig.close()
        return rig

    rig = run(go())
    assert rig.call.capture_stalls == 1
    assert any(n["type"] == "notice" and "stopped sending microphone audio" in n["message"] for n in rig.notes)
    assert "capture_stalled_during_playback" in [e["event"] for e in rig.rows("events.jsonl")]


def test_a_page_warning_is_shown_and_logged(tmp_path):
    async def go():
        rig = Rig(tmp_path)
        await rig.start()
        await rig.call.on_page_message({"type": "page_warning", "code": "audio_suspended", "message": "The browser blocked audio output."})
        await rig.close()
        return rig

    rig = run(go())
    assert any(n["type"] == "notice" and "blocked audio output" in n["message"] for n in rig.notes)
    assert "page_warning" in [e["event"] for e in rig.rows("events.jsonl")]


# ------------------------------------------------------------------ text to speech failing
def test_a_failed_line_is_marked_as_never_heard_even_though_the_brain_believes_it_said_it(tmp_path):
    async def go():
        rig = Rig(tmp_path, tts=FakeTTS(fail=TimbreUnavailable("still busy after 3 attempts")))
        await rig.start()
        await rig.say("u1", "namaste main Rajesh bol raha hoon")
        await rig.until(lambda: any(n["type"] == "notice" for n in rig.notes))
        await asyncio.sleep(0.1)
        await rig.close()
        return rig

    rig = run(go())
    row = rig.rows("turns.jsonl")[0]
    assert row["spoken"] is False and row["brain_believes_spoken"] is True and "still busy" in row["tts_error"] and row["uncle_text"]
    summary = json.loads((rig.folder() / "summary.json").read_text(encoding="utf-8"))
    assert summary["unspoken_turns"] == 1 and summary["uncle_lines_spoken"] == 0
    assert "tts_failed" in [e["event"] for e in rig.rows("events.jsonl")] and rig.audio == []
    assert not any(n["type"] == "mic" for n in rig.notes)                                  # nothing was spoken, so no mute


def test_three_failed_lines_in_a_row_end_the_call(tmp_path):
    async def go():
        rig = Rig(tmp_path, tts=FakeTTS(fail=TimbreUnavailable("down")))
        await rig.start()
        for i in range(3):
            await rig.say(f"u{i}", f"pehli doosri baat {['ek', 'do', 'teen'][i]} suniye ji")
            await rig.until(lambda n=i + 1: len(rig.turns()) == n or rig.call.done.is_set())
            await asyncio.sleep(0.1)
        await asyncio.wait_for(rig.call.done.wait(), 3)
        await rig.close()
        return rig

    rig = run(go())
    assert rig.call.end_reason == "tts_failed" and rig.call.unspoken_turns == 3


def test_a_plan_that_somehow_skipped_the_gate_is_never_spoken(tmp_path):
    async def go():
        rig = Rig(tmp_path)
        await rig.start()
        rig.call.brain.gate = lambda plan, state, ctx: plan                                 # simulate forgetting the gate
        await rig.say("u1", "namaste main Rajesh bol raha hoon")
        await rig.until(lambda: any(n["type"] == "notice" for n in rig.notes))
        await rig.close()
        return rig

    rig = run(go())
    assert rig.tts.texts == [] and rig.audio == []
    assert "ungated_plan_refused" in [e["event"] for e in rig.rows("events.jsonl")]
    assert rig.rows("turns.jsonl")[0]["spoken"] is False


def test_no_gate_no_live_call(tmp_path):
    rig_cfg = replace(VoiceConfig(), ledger_path=tmp_path / "c.json", calls_dir=tmp_path / "calls")

    async def notify(m): pass

    with pytest.raises(GateRequired):
        LiveCall(rig_cfg, None, Ledger(rig_cfg), notify, key=KEY, send_audio=None, tts_client=FakeTTS(), cache=AudioCache(rig_cfg))


# ------------------------------------------------------------------ the limits
def goodbye_lines() -> set[str]:
    brain = CallBrain(seed=0)
    return {l.text for l in brain.bank.tactic_lines("goodbye_wrap")}


def test_at_the_time_limit_uncle_says_one_in_persona_goodbye_through_the_gate_and_the_call_ends(tmp_path):
    async def go():
        rig = Rig(tmp_path, max_call_s=0.6)
        await rig.start()
        await rig.say("u1", "namaste main Rajesh bol raha hoon")
        await asyncio.wait_for(rig.call.done.wait(), 8)
        return rig

    rig = run(go())
    assert rig.call.end_reason == "time_limit"
    assert rig.tts.texts[-1] in goodbye_lines() and len(rig.tts.texts) == 2                    # one reply, then exactly one goodbye
    forced = [r for r in rig.rows("turns.jsonl") if r.get("forced")]
    assert forced and forced[0]["forced"] == "time_limit" and forced[0]["gated"] is True and forced[0]["spoken"] is True
    assert rig.notes[-1]["type"] == "ended" and rig.notes[-1]["reason"] == "time_limit"


def test_at_the_credit_hard_stop_one_goodbye_is_allowed_past_the_limit(tmp_path):
    async def go():
        rig = Rig(tmp_path, pool_credits=100.0, hard_stop_fraction=0.99, stt_credits_per_min=60.0, ledger_spent=98.9)
        assert not rig.ledger.blocked()
        await rig.start()
        await asyncio.wait_for(rig.call.done.wait(), 8)
        return rig

    rig = run(go())
    assert rig.call.end_reason == "credit_limit" and rig.ledger.blocked() and rig.ledger.spent < 100.0
    assert len(rig.tts.texts) == 1 and rig.tts.texts[0] in goodbye_lines()                    # spoken although the hard stop was passed
    assert rig.rows("turns.jsonl")[-1]["spoken"] is True


def test_when_the_whole_pool_is_spent_the_call_ends_silently(tmp_path):
    async def go():
        rig = Rig(tmp_path, pool_credits=100.0, hard_stop_fraction=0.999, stt_credits_per_min=3000.0, ledger_spent=99.8)
        await rig.start()
        await asyncio.wait_for(rig.call.done.wait(), 8)
        return rig

    rig = run(go())
    assert rig.call.end_reason == "credit_limit" and rig.ledger.spent >= 100.0
    assert rig.audio == [] and rig.notes[-1]["type"] == "ended"
    assert rig.rows("turns.jsonl")[-1]["spoken"] is False


def test_the_per_call_cap_also_gets_one_goodbye(tmp_path):
    async def go():
        rig = Rig(tmp_path, stt_credits_per_min=60.0, per_call_cap_credits=0.15)
        await rig.start()
        await asyncio.wait_for(rig.call.done.wait(), 8)
        return rig

    rig = run(go())
    assert rig.call.end_reason == "call_credit_cap" and len(rig.tts.texts) == 1 and rig.tts.texts[0] in goodbye_lines()


def test_a_goodbye_that_cannot_finish_inside_its_deadline_still_ends_the_call(tmp_path):
    async def go():
        rig = Rig(tmp_path, tts=FakeTTS(hang=True), goodbye_deadline_s=0.4, max_call_s=0.3)
        await rig.start()
        await asyncio.wait_for(rig.call.done.wait(), 8)
        return rig

    rig = run(go())
    assert rig.call.end_reason == "time_limit" and "goodbye_deadline" in [e["event"] for e in rig.rows("events.jsonl")]


def test_hanging_up_in_the_middle_of_uncles_line_is_clean(tmp_path):
    async def go():
        rig = Rig(tmp_path, autopage=False, tts=FakeTTS(seconds=0.4))
        await rig.start()
        await rig.say("u1", "namaste main Rajesh bol raha hoon")
        await rig.until(lambda: "mic" in rig.kinds())
        await rig.close()
        return rig

    rig = run(go())
    assert rig.call.end_reason == "hangup" and rig.notes[-1]["type"] == "ended"
    assert rig.rows("turns.jsonl")[0]["interrupted"] in (True, False)                         # a record exists either way


def test_the_summary_has_the_findings_the_stage_timeline_and_the_gate_decisions(tmp_path):
    async def go():
        rig = Rig(tmp_path)
        await rig.start()
        await rig.say("u1", "main CBI se bol raha hoon aapke parcel mein drugs mile hain case number hai chaar saat do nau ek")
        await rig.until(lambda: "latency" in rig.kinds())
        await rig.close()
        return rig

    rig = run(go())
    summary = json.loads((rig.folder() / "summary.json").read_text(encoding="utf-8"))
    assert summary["findings"]["case"] == ["47291"] and summary["turns"] == 1 and summary["gate_log"]
    assert json.loads((rig.folder() / "findings.json").read_text(encoding="utf-8"))["case"][0]["quote"]
    assert summary["final_stage"] in ("S0", "S1", "S2", "S3", "S4", "S5") and "stage_timeline" in summary
