"""The evidence packet (JSON), built from a saved call folder. Rules only: no model, no network, nothing is sent anywhere.

    python -m mirage.packet build data/calls/<call id> [more folders] [--out data/packets]
    python -m mirage.packet verify data/packets/<call id>.json [more files]

It follows the blueprint's fields (call_id, duration_s, language, scenario, intel with confidence, the verbatim quote and its time offset
t, payment_intel, stage_timeline with from_s and to_s, transcript_native, an integrity block with sha256 and generated_at in +05:30), and
adds what an investigator needs to read it honestly: a staged-demo label, how the call ended (caller, goodbye, exit rule, limit), what
Uncle disclosed, where the numbers come from, and the limits of the data.

Honesty rules, each tested:
  - every packet says it is a STAGED DEMO with SYNTHETIC data;
  - intel is copied from the call's own findings.json: values as heard, the extractor's own confidence label, needs_confirmation, validity
    and the exact quote; nothing is upgraded, rescored or turned into a number;
  - transcript_latin is null when only codemix text exists (no transliteration is invented), with the reason;
  - the sha256 covers the whole packet except the integrity block; `verify` recomputes it, and also checks that every intel quote
    really occurs in the transcript;
  - no claim about legal admissibility.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mirage.config import DATA_DIR, VoiceConfig

SCHEMA = "mirage-evidence-packet/0.1"
IST = timezone(timedelta(hours=5, minutes=30))
LABEL = ("STAGED DEMO: role-played call with SYNTHETIC data. The caller is a volunteer, not a real scammer; Uncle's name, phone number, "
         "account and bank are invented. Nothing in this packet describes a real person or a real crime.")
PAYMENT_TYPES = {"vpa": "upi_vpa", "account": "account_number", "ifsc": "ifsc"}
CONFIDENCE_LABELS = {
    "keyword": "a specific word (case, account, phone ...) came just before the number",
    "weak_keyword": "only the bare word 'number' came before it",
    "expected": "no keyword; Uncle had just asked for that kind of number, or the caller's label ended the previous turn",
    "length": "no keyword; the kind is a guess from the number of digits",
    "none": "no keyword and no usable length: kind unknown ('ambiguous')",
    "heard": "an id or code as heard (UPI id, IFSC, spelled letters)",
}
SOURCE_FILES = ("summary.json", "events.jsonl", "transcripts.jsonl", "turns.jsonl", "findings.json")
LATIN_REASON = ("Not available. These calls were transcribed with Prisma's 'codemix' format only (Devanagari and Latin mixed, as the service "
                "returned it). A Latin-letter copy needs the service's own transliteration, which was not requested, and no transliteration "
                "has been invented here.")
POLICY = ("Uncle is an automated agent and never says he is a person. A sincere 'are you an AI?' gets the honest answer at once when the call "
          "shows little evidence of a scam; with strong evidence he deflects up to twice in character, then answers honestly. This is the "
          "project's own policy, not a legal opinion.")


# --------------------------------------------------------------------------- hashing

def canonical(obj) -> bytes:
    """The bytes that are hashed: JSON, UTF-8, keys sorted, no spaces, no ASCII escaping."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def body_hash(packet: dict) -> str:
    return hashlib.sha256(canonical({k: v for k, v in packet.items() if k != "integrity"})).hexdigest()


# --------------------------------------------------------------------------- reading the call folder

def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _r(x, n=2):
    return None if x is None else round(float(x), n)


def stage_segments(moves: list[dict], turns: list[dict], final_stage: str, duration: float) -> list[dict]:
    reasons_by_turn = {t.get("turn"): t.get("stage_reasons", []) for t in turns}
    segs, stage, start, entered = [], "S0", 0.0, None
    for m in sorted(moves, key=lambda m: m["t"]):
        seg = {"stage": stage, "from_s": _r(start), "to_s": _r(m["t"])}
        if entered:
            seg.update(entered)
        segs.append(seg)
        stage, start = m["to"], m["t"]
        entered = {"entered_on_turn": m.get("turn"), "entered_because": list(reasons_by_turn.get(m.get("turn"), []))}
    seg = {"stage": stage, "from_s": _r(start), "to_s": _r(duration)}
    if entered:
        seg.update(entered)
    segs.append(seg)
    return segs


def classify_ending(summary: dict, turns: list[dict]) -> dict:
    raw = summary.get("end_reason", "")
    last = turns[-1] if turns else {}
    tactic = last.get("tactic", "")
    out = {"end_reason_raw": raw, "ended_at_s": _r(summary.get("length_s")), "last_turn_tactic": tactic or None,
           "last_turn_stage_reasons": list(last.get("stage_reasons", [])) if raw == "uncle_ended" else []}
    if raw == "hangup":
        out.update(type="caller_side_hangup", detail="The call page's Hang up ended the call. The log cannot say who pressed it; in staged calls "
                   "the person playing the caller sits at the same computer.")
    elif raw in ("time_limit", "credit_limit", "call_credit_cap"):
        out.update(type="limit", limit=raw, uncle_said_goodbye=bool(last.get("forced")),
                   detail="The call reached a limit set by the project. Uncle says one in-persona goodbye when he can.")
    elif raw in ("uncle_ended", "gate_ended"):
        kinds = {"gate_exit_polite": ("exit_rule", "Uncle ended the call under the exit rule: after the grace period the call still showed "
                                                   "little evidence of a scam, so he said he is an automated assistant and ended it."),
                 "gate_disclose_identity": ("identity_disclosure", "Uncle was sincerely asked whether he is a person or an AI, answered honestly "
                                                                   "that he is an automated assistant, and ended the call."),
                 "gate_disclose_emergency": ("emergency_disclosure", "A possible real emergency was raised; Uncle said he is an automated "
                                                                     "assistant, gave the emergency number and ended the call."),
                 "gate_hostile_goodbye": ("hostile_goodbye", "The caller was abusive; Uncle said goodbye and ended the call."),
                 "goodbye_wrap": ("goodbye", "Uncle said his in-persona goodbye because the caller was wrapping up (for example, announced a hang-up)."),
                 "gate_call_over": ("gate_ended", "The safety gate ended the call without a spoken line.")}
        kind, detail = kinds.get(tactic, ("uncle_ended_other", "Uncle ended the call; see last_turn_tactic."))
        out.update(type=kind, detail=detail)
    else:
        out.update(type="other", detail=f"The call ended for this reason: {raw or 'not recorded'}.")
    return out


def disclosure_block(summary: dict, uncle_lines: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for g in summary.get("gate_log", []):
        counts[g.get("action", "?")] = counts.get(g.get("action", "?"), 0) + 1
    said = [u for u in uncle_lines if "automated assistant" in u["text"].lower()]
    return {
        "uncle_is_automated": True,
        "identity_questions": {"deflected": counts.get("deflect_identity", 0), "answered_honestly": counts.get("disclose_identity", 0)},
        "emergency_disclosures": counts.get("disclose_emergency", 0),
        "said_automated_assistant": bool(said),
        "first_said_at_s": said[0]["t"] if said else None,
        "gate_actions": {k: v for k, v in sorted(counts.items()) if k != "pass"},
        "policy": POLICY,
    }


def _overlaps(items: list[dict]) -> None:
    """Point from an item to a longer item of the same kind that contains its value. A pointer only: nothing is merged or removed."""
    for a in items:
        longer = [b["value"] for b in items if b is not a and b["kind"] == a["kind"] and len(b["value"]) > len(a["value"]) and a["value"] in b["value"]]
        if longer:
            a["possibly_part_of"] = longer[0]


def build_packet(call_dir: Path, *, audio_dir: Path | None = None, now: datetime | None = None) -> dict:
    call_dir = Path(call_dir)
    summary = json.loads((call_dir / "summary.json").read_text(encoding="utf-8"))
    transcripts = _jsonl(call_dir / "transcripts.jsonl")
    events = _jsonl(call_dir / "events.jsonl")
    turns = _jsonl(call_dir / "turns.jsonl")
    findings_path = call_dir / "findings.json"
    findings = json.loads(findings_path.read_text(encoding="utf-8")) if findings_path.exists() else {}
    call_id = summary["call_id"]
    duration = float(summary["length_s"])
    now = now or datetime.now(IST)
    generated_at = now.astimezone(IST).isoformat(timespec="seconds")

    native = [{"t": _r(r["t"], 3), "utterance_id": r.get("utterance_id"), "language": r.get("language") or (r.get("raw") or {}).get("detected_language_code"),
               "audio_s": _r(r.get("audio_s"), 3), "text": r["text"]} for r in transcripts if r.get("text", "").strip()]
    langs: dict[str, int] = {}
    for n in native:
        if n["language"]:
            langs[n["language"]] = langs.get(n["language"], 0) + 1

    tactic_by_turn = {t.get("turn"): t.get("tactic") for t in turns}
    uncle = [{"t": _r(e["t"], 3), "turn": e.get("turn"), "text": e.get("text", ""), "tactic": tactic_by_turn.get(e.get("turn"))}
             for e in events if e.get("event") == "uncle_speaking_start"]

    intel = []
    for kind, items in findings.items():
        for f in items:
            intel.append({"kind": f["kind"], "value": f["value"], "confidence": f["confidence"],
                          "needs_confirmation": f["needs_confirmation"], "valid": f["valid"], "reason": f.get("reason", ""),
                          "candidates": list(f.get("candidates") or []), "suggested": f.get("suggested"),
                          "quote": f["quote"], "t": _r(f["t"], 3)})
    intel.sort(key=lambda i: (i["t"], i["kind"], i["value"]))
    _overlaps(intel)
    payment = [{"type": PAYMENT_TYPES[i["kind"]], **i} for i in intel if i["kind"] in PAYMENT_TYPES]
    by_conf: dict[str, int] = {}
    for i in intel:
        by_conf[i["confidence"]] = by_conf.get(i["confidence"], 0) + 1

    muted_s = round(summary.get("audio", {}).get("muted_frames", 0) * 1024 / 16000, 1)   # 1,024 bytes = 512 samples at 8 kHz = 64 ms
    audio_dir = audio_dir if audio_dir is not None else VoiceConfig().audio_dir
    wav = Path(audio_dir) / call_id / "mic_8k.wav"
    audio = {"present": wav.exists()}
    if wav.exists():
        audio.update(file="mic_8k.wav", bytes=wav.stat().st_size, sha256=sha256_file(wav),
                     note="Raw audio is not part of the packet; this is only the hash of the file that was saved, if it still exists.")
    packet = {
        "schema": SCHEMA,
        "label": LABEL,
        "data_status": {"staged": True, "synthetic": True, "real_scammer": False},
        "call_id": call_id,
        "started_at": summary.get("started_at"),
        "duration_s": _r(duration, 1),
        "language": "hi-IN",
        "languages_detected": langs,
        "transcript_format": "codemix",
        "scenario": summary.get("scenario", "undecided"),
        "scenario_note": "The scenario is the project's own keyword guess (digital_arrest, bank_kyc), or 'undecided'; it is not a finding about the caller.",
        "agent": {"name": "Rajesh Uncle (fictional persona)", "is_automated": True},
        "intel": intel,
        "intel_summary": {"count": len(intel), "by_confidence": dict(sorted(by_conf.items())),
                          "needs_confirmation": sum(1 for i in intel if i["needs_confirmation"]),
                          "confidence_is": "the extractor's own label, copied from the call's findings; not a probability, and nothing has been upgraded or rescored",
                          "labels": {k: v for k, v in CONFIDENCE_LABELS.items() if k in by_conf},
                          "t_is": "seconds from the start of the call at which the transcript carrying the quote arrived; not when the words were spoken"},
        "payment_intel": payment,
        "payment_intel_note": None if payment else "No UPI id, account number or IFSC code was captured in this call.",
        "stage_timeline": stage_segments(summary.get("stage_timeline", []), turns, summary.get("final_stage", "S0"), duration),
        "stage_timeline_note": "Stages and intel are as the brain recorded them DURING the call, by the rules in force then; a later change to the rules does not alter a saved call. A stage starts at the time of Uncle's reply on the turn where the keyword rules moved it, not when the caller said the words; 'entered_because' lists the rule hits.",
        "transcript_native": native,
        "transcript_latin": None,
        "transcript_latin_reason": LATIN_REASON,
        "uncle_lines": uncle,
        "capture": {"caller_audio_muted_s": muted_s,
                    "note": "While Uncle's voice played, the microphone audio sent to the speech service was silenced, so nothing the caller said during those seconds is in the transcript."},
        "disclosure": disclosure_block(summary, uncle),
        "ending": classify_ending(summary, turns),
        "audio": audio,
        "source_files": {n: sha256_file(call_dir / n) for n in SOURCE_FILES if (call_dir / n).exists()},
        "limitations": [
            "Staged, role-played call; it says nothing about real scammers.",
            "The transcript is the speech service's codemix text as received. Digits and words can be misheard; items with needs_confirmation true were not confirmed by the caller.",
            "Intel values are as heard. A value that is a piece of a longer one of the same kind is flagged possibly_part_of, not merged.",
            "Times are seconds from the start of the call as measured by the server.",
        ],
        "generated_at": generated_at,
    }
    packet["integrity"] = {
        "sha256": body_hash(packet), "generated_at": generated_at, "algorithm": "sha256",
        "covers": "the canonical JSON of this packet without the integrity block (generated_at is inside the covered part)",
        "canonicalization": "UTF-8 JSON, keys sorted, separators ',' and ':', no ASCII escaping",
        "verify": "python -m mirage.packet verify <this file>",
        "note": "The hash only shows that this file is unchanged since it was generated. No claim is made about legal admissibility.",
    }
    return packet


# --------------------------------------------------------------------------- writing

ONE_LINE_PER_ITEM = ("transcript_native", "uncle_lines")


def format_packet(packet: dict) -> str:
    """Readable JSON: indented, with each transcript segment on one line. Only whitespace differs from json.dumps(indent=2); the hash is
    over the canonical form, so it does not depend on this layout."""
    nl = chr(10)
    parts = []
    for key, value in packet.items():
        if key in ONE_LINE_PER_ITEM and isinstance(value, list) and value:
            body = ("," + nl).join("    " + json.dumps(item, ensure_ascii=False) for item in value)
            parts.append("  " + json.dumps(key) + ": [" + nl + body + nl + "  ]")
        else:
            text = json.dumps(value, indent=2, ensure_ascii=False).replace(nl, nl + "  ")
            parts.append("  " + json.dumps(key) + ": " + text)
    return "{" + nl + ("," + nl).join(parts) + nl + "}" + nl


# --------------------------------------------------------------------------- verifying

def _quote_pieces(quote: str) -> list[str]:
    return [p.strip() for p in quote.split(" | ") if p.strip()]


def verify_packet(packet: dict) -> list[str]:
    """Problems found; an empty list means the packet verifies."""
    problems = []
    integ = packet.get("integrity")
    if not isinstance(integ, dict) or "sha256" not in integ:
        return ["no integrity block"]
    if body_hash(packet) != integ["sha256"]:
        problems.append("sha256 does not match: the packet was changed after it was generated")
    if integ.get("generated_at") != packet.get("generated_at"):
        problems.append("generated_at differs between the packet and its integrity block")
    if not str(packet.get("generated_at", "")).endswith("+05:30"):
        problems.append("generated_at is not in +05:30")
    if packet.get("data_status", {}).get("synthetic") is not True or "STAGED DEMO" not in packet.get("label", ""):
        problems.append("the staged-demo / synthetic label is missing")
    texts = [n["text"] for n in packet.get("transcript_native", [])]
    for item in packet.get("intel", []):
        for piece in _quote_pieces(item["quote"]):
            if not any(piece in t for t in texts):
                problems.append(f"intel {item['kind']} {item['value']!r}: quote piece {piece!r} is not in the transcript")
    return problems


def verify_file(path: Path) -> list[str]:
    return verify_packet(json.loads(Path(path).read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- command line

def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Build or verify an evidence packet (offline).")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build a packet from one or more data/calls/<id> folders")
    b.add_argument("calls", nargs="+", type=Path)
    b.add_argument("--out", type=Path, default=DATA_DIR / "packets")
    v = sub.add_parser("verify", help="recompute the hash and check the quotes of one or more packets")
    v.add_argument("files", nargs="+", type=Path)
    args = p.parse_args(argv)
    if args.cmd == "build":
        args.out.mkdir(parents=True, exist_ok=True)
        for folder in args.calls:
            packet = build_packet(folder)
            path = args.out / f"{packet['call_id']}.json"
            path.write_text(format_packet(packet), encoding="utf-8")
            s = packet["intel_summary"]
            print(f"{packet['call_id']}: {s['count']} intel item(s) {s['by_confidence']}, {len(packet['payment_intel'])} payment item(s), "
                  f"ended: {packet['ending']['type']} -> {path}")
        return 0
    bad = 0
    for f in args.files:
        problems = verify_file(f)
        print(f"{f}: {'OK' if not problems else 'FAILED'}")
        for pr in problems:
            print("   -", pr)
        bad += bool(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
