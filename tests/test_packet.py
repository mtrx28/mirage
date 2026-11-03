"""The evidence packet (mirage/packet.py): honest, tamper-evident, offline. The call folder here is INVENTED (a staged call), written in the
format the live call saves. Nothing touches the network."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from mirage import packet as pk

NOW = datetime(2026, 11, 5, 4, 32, 11, tzinfo=timezone.utc)        # 10:02:11 in +05:30


def f(kind, value, confidence, quote, t, needs=True, valid=True, reason="", candidates=()):
    return {"kind": kind, "value": value, "confidence": confidence, "valid": valid, "reason": reason, "needs_confirmation": needs,
            "quote": quote, "t": t, "suggested": None, "candidates": list(candidates)}


TRANSCRIPTS = [
    (7.0, "Hello sir, customs office से बोल रहा हूँ।", "hi-IN"),
    (20.5, "Case reference 0000 | एक, दो, तीन, चार है।", "hi-IN"),
    (31.0, "एक, दो, तीन, चार।", "hi-IN"),
    (50.0, "मेरा नंबर है 5 5 5 1 2 3 4 5 6 7", "en-IN"),
    (60.0, "ठीक है sir, call disconnect करता हूँ।", "hi-IN"),
]
FINDINGS = {
    "case": [f("case", "00001234", "keyword", "0000 | एक, दो, तीन, चार", 20.5, needs=False),
             f("case", "1234", "expected", "एक, दो, तीन, चार", 31.0)],
    "ambiguous": [f("ambiguous", "5551234567", "none", "5 5 5 1 2 3 4 5 6 7", 50.0, valid=True, candidates=("phone", "account"))],
    "vpa": [f("vpa", "ravi.test@mirageupi", "heard", "ravi dot test at mirage UPI", 45.0, valid=True)],
}


def make_folder(tmp_path, *, end_reason="hangup", last_tactic="generic_stall", gate_log=None, findings=None, moves=None, forced=False, with_vpa_quote=True):
    d = tmp_path / "calls" / "c1"
    d.mkdir(parents=True)
    findings = FINDINGS if findings is None else findings
    transcripts = TRANSCRIPTS + ([(45.0, "ravi dot test at mirage UPI", "en-IN")] if with_vpa_quote else [])
    (d / "transcripts.jsonl").write_text("\n".join(json.dumps({"t": t, "text": x, "audio_s": 2.0, "language": lang, "utterance_id": f"u{i}", "raw": {}},
                                                              ensure_ascii=False) for i, (t, x, lang) in enumerate(transcripts)) + "\n", encoding="utf-8")
    ev = [{"t": 0.0, "event": "start"}, {"t": 8.0, "event": "uncle_speaking_start", "turn": 1, "text": "<coughs> Haan ji boliye beta."},
          {"t": 62.0, "event": "uncle_speaking_start", "turn": 2, "text": "Main ek automated assistant hoon, asli insaan nahin."},
          {"t": 70.0, "event": "end", "reason": end_reason}]
    (d / "events.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in ev) + "\n", encoding="utf-8")
    turns = [{"turn": 1, "tactic": "greet_confused", "stage_reasons": ["S0: customs +1"], "stage": "S0"},
             {"turn": 2, "tactic": last_tactic, "stage_reasons": ["S5: disconnect +2"], "stage": "S5", **({"forced": "time_limit"} if forced else {})}]
    (d / "turns.jsonl").write_text("\n".join(json.dumps(t) for t in turns) + "\n", encoding="utf-8")
    (d / "findings.json").write_text(json.dumps(findings, ensure_ascii=False), encoding="utf-8")
    summary = {"call_id": "c1", "started_at": "2026-11-02T16:01:44+00:00", "length_s": 70.0, "end_reason": end_reason,
               "scenario": "digital_arrest", "final_stage": "S5", "gate_log": gate_log or [{"turn": 0, "action": "pass", "why": "", "tactic": "greet_confused"}],
               "stage_timeline": moves if moves is not None else [{"t": 30.0, "turn": 1, "from": "S0", "to": "S1"}, {"t": 61.0, "turn": 2, "from": "S1", "to": "S5"}],
               "audio": {"muted_frames": 250}}
    (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return d


def build(tmp_path, **kw):
    d = make_folder(tmp_path, **kw)
    return pk.build_packet(d, audio_dir=tmp_path / "audio", now=NOW)


# ---------------------------------------------------------------- the fields

def test_the_packet_has_the_blueprint_fields_and_a_staged_synthetic_label(tmp_path):
    p = build(tmp_path)
    for key in ("call_id", "duration_s", "language", "scenario", "intel", "payment_intel", "stage_timeline", "transcript_native", "integrity"):
        assert key in p
    assert p["call_id"] == "c1" and p["duration_s"] == 70.0 and p["language"] == "hi-IN" and p["scenario"] == "digital_arrest"
    assert "STAGED DEMO" in p["label"] and "SYNTHETIC" in p["label"]
    assert p["data_status"] == {"staged": True, "synthetic": True, "real_scammer": False}
    assert p["languages_detected"] == {"hi-IN": 4, "en-IN": 2}
    assert p["integrity"]["generated_at"] == "2026-11-05T10:02:11+05:30" == p["generated_at"]


def test_transcript_native_is_the_verbatim_timed_text_and_uncle_lines_are_kept_apart(tmp_path):
    p = build(tmp_path)
    assert [n["text"] for n in p["transcript_native"]][:2] == [TRANSCRIPTS[0][1], TRANSCRIPTS[1][1]]
    assert all({"t", "text", "language", "audio_s"} <= set(n) for n in p["transcript_native"])
    assert [u["turn"] for u in p["uncle_lines"]] == [1, 2] and p["uncle_lines"][0]["tactic"] == "greet_confused"


def test_transcript_latin_is_null_with_the_reason_and_nothing_is_transliterated(tmp_path):
    p = build(tmp_path)
    assert p["transcript_latin"] is None
    assert "codemix" in p["transcript_latin_reason"] and "no transliteration" in p["transcript_latin_reason"].lower()


def test_stage_timeline_is_contiguous_from_zero_to_the_duration_with_the_words_that_moved_it(tmp_path):
    p = build(tmp_path)
    tl = p["stage_timeline"]
    assert [(s["stage"], s["from_s"], s["to_s"]) for s in tl] == [("S0", 0.0, 30.0), ("S1", 30.0, 61.0), ("S5", 61.0, 70.0)]
    assert tl[2]["entered_because"] == ["S5: disconnect +2"] and tl[2]["entered_on_turn"] == 2
    assert [s["stage"] for s in build(tmp_path / "x", moves=[]).get("stage_timeline")] == ["S0"]


# ---------------------------------------------------------------- honest confidence

def test_intel_is_copied_as_recorded_nothing_upgraded_nothing_turned_into_a_number(tmp_path):
    p = build(tmp_path)
    items = {(i["kind"], i["value"]): i for i in p["intel"]}
    assert items[("case", "00001234")]["confidence"] == "keyword" and items[("case", "00001234")]["needs_confirmation"] is False
    exp = items[("case", "1234")]
    assert exp["confidence"] == "expected" and exp["needs_confirmation"] is True                     # stays "expected"
    amb = items[("ambiguous", "5551234567")]
    assert amb["confidence"] == "none" and amb["candidates"] == ["phone", "account"] and amb["needs_confirmation"] is True
    assert all(isinstance(i["confidence"], str) for i in p["intel"])                                # labels, never numbers
    assert all(i["value"] == next(x["value"] for x in FINDINGS[i["kind"]] if x["value"] == i["value"]) for i in p["intel"])
    assert {i["quote"] for i in p["intel"]} == {x["quote"] for v in FINDINGS.values() for x in v}      # the exact quotes
    assert p["intel_summary"]["count"] == 4 and p["intel_summary"]["by_confidence"] == {"expected": 1, "heard": 1, "keyword": 1, "none": 1}


def test_a_value_that_is_a_piece_of_a_longer_one_is_pointed_out_not_merged(tmp_path):
    p = build(tmp_path)
    items = {(i["kind"], i["value"]): i for i in p["intel"]}
    assert items[("case", "1234")]["possibly_part_of"] == "00001234" and "possibly_part_of" not in items[("case", "00001234")]
    assert p["intel_summary"]["count"] == 4


def test_payment_intel_holds_only_payment_kinds_and_says_so_when_empty(tmp_path):
    p = build(tmp_path)
    assert [(i["type"], i["value"]) for i in p["payment_intel"]] == [("upi_vpa", "ravi.test@mirageupi")]
    empty = build(tmp_path / "e", findings={"case": FINDINGS["case"]})
    assert empty["payment_intel"] == [] and "No UPI id" in empty["payment_intel_note"]


# ---------------------------------------------------------------- how the call ended, and what Uncle disclosed

@pytest.mark.parametrize("end_reason, tactic, forced, expected", [
    ("hangup", "generic_stall", False, "caller_side_hangup"),
    ("uncle_ended", "goodbye_wrap", False, "goodbye"),
    ("uncle_ended", "gate_exit_polite", False, "exit_rule"),
    ("uncle_ended", "gate_disclose_identity", False, "identity_disclosure"),
    ("uncle_ended", "gate_disclose_emergency", False, "emergency_disclosure"),
    ("uncle_ended", "gate_hostile_goodbye", False, "hostile_goodbye"),
    ("time_limit", "goodbye_wrap", True, "limit"),
    ("credit_limit", "goodbye_wrap", False, "limit"),
    ("tts_failed", "generic_stall", False, "other"),
])
def test_how_the_call_ended_is_classified_from_the_log(tmp_path, end_reason, tactic, forced, expected):
    p = build(tmp_path, end_reason=end_reason, last_tactic=tactic, forced=forced)
    assert p["ending"]["type"] == expected and p["ending"]["end_reason_raw"] == end_reason and p["ending"]["detail"]
    if expected == "limit":
        assert p["ending"]["limit"] == end_reason and p["ending"]["uncle_said_goodbye"] is forced


def test_the_disclosure_block_counts_what_the_gate_did(tmp_path):
    log = [{"turn": 0, "action": "pass"}, {"turn": 1, "action": "deflect_identity"}, {"turn": 2, "action": "deflect_identity"},
           {"turn": 3, "action": "disclose_identity"}]
    d = build(tmp_path, gate_log=log)["disclosure"]
    assert d["identity_questions"] == {"deflected": 2, "answered_honestly": 1} and d["uncle_is_automated"] is True
    assert d["said_automated_assistant"] is True and d["first_said_at_s"] == 62.0
    assert d["gate_actions"] == {"deflect_identity": 2, "disclose_identity": 1} and "not a legal opinion" in d["policy"]


# ---------------------------------------------------------------- integrity

def test_a_fresh_packet_verifies_and_building_twice_gives_identical_bytes(tmp_path):
    p = build(tmp_path)
    assert pk.verify_packet(p) == []
    again = pk.build_packet(tmp_path / "calls" / "c1", audio_dir=tmp_path / "audio", now=NOW)
    assert pk.canonical(p) == pk.canonical(again)


def test_the_hash_is_over_the_canonical_packet_without_the_integrity_block(tmp_path):
    import hashlib
    p = build(tmp_path)
    body = {k: v for k, v in p.items() if k != "integrity"}
    expected = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    assert p["integrity"]["sha256"] == expected
    reordered = {k: p[k] for k in reversed(list(p))}                       # key order in the file does not matter
    assert pk.verify_packet(reordered) == []


@pytest.mark.parametrize("tamper", [
    lambda p: p["intel"][0].__setitem__("value", "99999999"),
    lambda p: p["intel"][1].__setitem__("confidence", "keyword"),                    # upgrading a confidence is caught
    lambda p: p["transcript_native"][0].__setitem__("text", "something else"),
    lambda p: p["stage_timeline"][0].__setitem__("to_s", 1.0),
    lambda p: p["ending"].__setitem__("type", "exit_rule"),
    lambda p: p.__setitem__("duration_s", 71.0),
    lambda p: p["data_status"].__setitem__("real_scammer", True),
    lambda p: p.__setitem__("generated_at", "2026-11-05T10:02:12+05:30"),            # the timestamp is covered too
])
def test_changing_anything_in_the_packet_breaks_the_hash(tmp_path, tamper):
    p = build(tmp_path)
    tamper(p)
    assert pk.verify_packet(p)


def test_a_replaced_hash_or_a_different_timestamp_in_the_integrity_block_is_caught(tmp_path):
    p = build(tmp_path)
    q = copy.deepcopy(p)
    q["integrity"]["sha256"] = "0" * 64
    assert pk.verify_packet(q)
    q = copy.deepcopy(p)
    q["integrity"]["generated_at"] = "2026-11-05T10:02:12+05:30"
    assert any("generated_at" in x for x in pk.verify_packet(q))


def test_an_intel_quote_that_is_not_in_the_transcript_fails_verification(tmp_path):
    p = build(tmp_path, with_vpa_quote=False)                                       # the UPI finding's words are not in this transcript
    problems = pk.verify_packet(p)
    assert any("not in the transcript" in x for x in problems)


def test_generated_at_is_in_plus_0530_whatever_zone_the_clock_is_in(tmp_path):
    d = make_folder(tmp_path)
    p = pk.build_packet(d, audio_dir=tmp_path / "a", now=datetime(2026, 11, 5, 12, 0, tzinfo=timezone(timedelta(hours=-8))))
    assert p["generated_at"] == "2026-11-06T01:30:00+05:30"


def test_the_source_files_are_hashed_and_the_audio_is_only_hashed_if_it_exists(tmp_path):
    d = make_folder(tmp_path)
    (tmp_path / "audio" / "c1").mkdir(parents=True)
    (tmp_path / "audio" / "c1" / "mic_8k.wav").write_bytes(b"RIFFxxxx")
    p = pk.build_packet(d, audio_dir=tmp_path / "audio", now=NOW)
    assert set(p["source_files"]) == set(pk.SOURCE_FILES) and p["audio"]["present"] and len(p["audio"]["sha256"]) == 64
    assert p["audio"]["bytes"] == 8 and "RIFF" not in json.dumps(p)
    assert build(tmp_path / "n")["audio"] == {"present": False}


# ---------------------------------------------------------------- what the packet must not say

def test_no_claim_about_legal_admissibility_anywhere(tmp_path):
    text = json.dumps(build(tmp_path), ensure_ascii=False).lower()
    assert text.count("admissib") == 1                                     # only the sentence that disclaims it
    assert "no claim is made about legal admissibility" in text
    for banned in ("court-ready", "legally valid", "legally binding", "is admissible", "will be admissible", "certified"):
        assert banned not in text


def test_the_capture_note_says_what_the_half_duplex_mute_cost(tmp_path):
    cap = build(tmp_path)["capture"]
    assert cap["caller_audio_muted_s"] == 16.0 and "silenced" in cap["note"]        # 250 frames x 64 ms


# ---------------------------------------------------------------- the command line

def test_build_and_verify_commands(tmp_path, capsys):
    d = make_folder(tmp_path)
    out = tmp_path / "packets"
    assert pk.main(["build", str(d), "--out", str(out)]) == 0
    path = out / "c1.json"
    assert path.exists() and "4 intel item(s)" in capsys.readouterr().out
    assert pk.main(["verify", str(path)]) == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    data["intel"][0]["value"] = "1"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert pk.main(["verify", str(path)]) == 1
    assert "FAILED" in capsys.readouterr().out


def test_the_written_file_is_readable_json_that_still_verifies(tmp_path):
    p = build(tmp_path)
    text = pk.format_packet(p)
    assert json.loads(text) == p
    lines = [l for l in text.splitlines() if l.startswith('    {"t":')]
    assert len(lines) == len(p["transcript_native"]) + len(p["uncle_lines"])           # one segment per line
    assert pk.verify_packet(json.loads(text)) == []
