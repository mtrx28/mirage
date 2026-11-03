"""E1, the scoring side: how many spoken numbers did the extractor capture, exactly?

    python -m mirage.e1 <expected numbers file> <call id> [<call id> ...] [--calls-dir DIR] [--json OUT]

STAGED, SMALL-SAMPLE DEV DATA. About 20 numbers said by two people in a few live calls is a sanity check on where digit capture breaks, not the
Digit Fidelity benchmark in the blueprint (60 clips, noise levels, output modes), and it gives no rate for real callers.

It reads ONLY what each call logged: `data/calls/<id>/findings.json`, the intel the extractor stored during the call. It does not re-run the
extractor, so the result is what the system did at that time, with the rules then in force. Nothing is sent anywhere.

Expected file, one number per line (`#` starts a comment):

    number | label | style | speaker [| call id]

  label    what the extractor should call it: phone, case or account
  style    free text, usually one_digit_per_pause or natural_groups
  speaker  who said it (owner, friend ...)
  call id  optional: only look in that call (otherwise in all the calls given)

For each expected number the verdict is one of:
  exact        a finding with the same full value AND the same label
  wrong_label  a finding with the same full value but another label (for example `ambiguous` instead of `phone`)
  partial      no finding with the full value, but one shares a run of digits with it (a piece, a longer string that contains it, or a
               value with one digit wrong): at least 3 digits and at least half of the expected number
  missed       nothing close
A number counts as captured only if its FULL value matches (exact or wrong_label); "exact" also needs the right label.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from mirage.config import VoiceConfig

BANNER = ("STAGED, SMALL-SAMPLE DEV DATA. Not the Digit Fidelity benchmark and not a rate for real callers: a few dozen numbers, two speakers, "
          "live calls, whatever microphone and rules were in use then.")
LABELS = ("phone", "case", "account")
VERDICT_WORDS = {"exact": "captured exactly", "wrong_label": "captured, wrong label", "partial": "partly captured", "missed": "missed"}
ORDER = ("exact", "wrong_label", "partial", "missed")


@dataclass(frozen=True)
class Expected:
    id: str
    value: str                    # digits, leading zeros kept
    label: str
    style: str
    speaker: str
    call: str | None = None
    line_no: int = 0


@dataclass(frozen=True)
class Found:
    call: str
    kind: str
    value: str
    confidence: str
    quote: str
    t: float


@dataclass
class Result:
    expected: Expected
    verdict: str
    found: Found | None = None
    detail: str = ""


# --------------------------------------------------------------------------- reading the files

def parse_expected(path: Path) -> tuple[list[Expected], list[str]]:
    """The expected numbers, and warnings (for example the same number listed twice)."""
    items: list[Expected] = []
    warnings: list[str] = []
    for n, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) not in (4, 5):
            raise ValueError(f"line {n}: expected 'number | label | style | speaker [| call id]', got {raw.strip()!r}")
        digits = re.sub(r"[\s\-.,]", "", parts[0])
        if not digits.isdigit():
            raise ValueError(f"line {n}: the number must be digits only (spaces and hyphens are ignored), got {parts[0]!r}")
        if parts[1] not in LABELS:
            raise ValueError(f"line {n}: the label must be one of {LABELS}, got {parts[1]!r}")
        if not parts[2] or not parts[3]:
            raise ValueError(f"line {n}: style and speaker may not be empty")
        items.append(Expected(f"E{len(items) + 1:02d}", digits, parts[1], parts[2], parts[3], parts[4] if len(parts) == 5 and parts[4] else None, n))
    seen: dict[tuple[str, str | None], str] = {}
    for e in items:
        key = (e.value, e.call)
        if key in seen:
            warnings.append(f"{e.id} repeats {seen[key]} ({e.value}): the extractor keeps one finding per kind and value in a call, so only one of "
                            "them can be matched. Give them different call ids, or say each number once per call.")
        else:
            seen[key] = e.id
    return items, warnings


def load_findings(call_id: str, calls_dir: Path) -> list[Found]:
    path = Path(calls_dir) / call_id / "findings.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist (is the call id right? did the call finish?)")
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for items in data.values():
        for f in items:
            if str(f["value"]).isdigit():
                out.append(Found(call_id, f["kind"], f["value"], f.get("confidence", ""), f.get("quote", ""), float(f.get("t", 0.0))))
    return sorted(out, key=lambda f: (f.call, f.t, f.kind))


# --------------------------------------------------------------------------- comparing

def longest_common_run(a: str, b: str) -> int:
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def partial_threshold(value: str) -> int:
    return max(3, math.ceil(len(value) / 2))


def score(expected: list[Expected], findings: list[Found]) -> list[Result]:
    """One result per expected number, in file order. Each finding is used for at most one number (the first that fits)."""
    used: set[int] = set()
    results: list[Result] = []
    for e in expected:
        pool = [(i, f) for i, f in enumerate(findings) if i not in used and (e.call is None or f.call == e.call)]
        same = [(i, f) for i, f in pool if f.value == e.value]
        right = [(i, f) for i, f in same if f.kind == e.label]
        if right:
            i, f = right[0]
            used.add(i)
            results.append(Result(e, "exact", f))
        elif same:
            i, f = same[0]
            used.add(i)
            results.append(Result(e, "wrong_label", f, f"heard as {f.kind!r}, expected {e.label!r}"))
        else:
            need = partial_threshold(e.value)
            near = [(longest_common_run(e.value, f.value), -abs(len(f.value) - len(e.value)), i, f) for i, f in pool]
            near = [x for x in near if x[0] >= need]
            if near:
                run, _, i, f = max(near, key=lambda x: (x[0], x[1]))
                used.add(i)
                results.append(Result(e, "partial", f, f"shares a run of {run} of {len(e.value)} digits; stored {f.value!r} ({len(f.value)} digits), "
                                                       f"edit distance {edit_distance(e.value, f.value)}"))
            else:
                results.append(Result(e, "missed", None, "no stored number shares half of its digits"))
    return results


# --------------------------------------------------------------------------- summarising

def tally(results: list[Result]) -> dict:
    def counts(rs: list[Result]) -> dict:
        c = {v: sum(1 for r in rs if r.verdict == v) for v in ORDER}
        c["attempted"] = len(rs)
        c["full_value"] = c["exact"] + c["wrong_label"]
        return c
    out = {"all": counts(results)}
    for field_name in ("style", "speaker", "label"):
        keys = sorted({getattr(r.expected, field_name) for r in results})
        out[field_name] = {k: counts([r for r in results if getattr(r.expected, field_name) == k]) for k in keys}
    return out


def report(results: list[Result], warnings: list[str], calls: list[str]) -> str:
    lines = [BANNER, "", f"Calls: {', '.join(calls)}   (findings as logged during each call; the extractor was not re-run)", ""]
    for w in warnings:
        lines.append(f"WARNING: {w}")
    if warnings:
        lines.append("")
    lines.append(f"{'id':<4} {'expected':<16} {'label':<8} {'style':<22} {'speaker':<9} verdict")
    for r in results:
        e = r.expected
        where = f"  [{r.found.call} t={r.found.t}]" if r.found else ""
        lines.append(f"{e.id:<4} {e.value:<16} {e.label:<8} {e.style:<22} {e.speaker:<9} {VERDICT_WORDS[r.verdict]}{where}")
    t = tally(results)
    a = t["all"]
    lines += ["", "Counts (exact = full value and right label; full value = exact + wrong label):",
              f"  ALL: full value {a['full_value']} of {a['attempted']} attempted; exact {a['exact']} of {a['attempted']}; "
              f"wrong label {a['wrong_label']}; partial {a['partial']}; missed {a['missed']}"]
    for field_name, title in (("style", "by style"), ("speaker", "by speaker"), ("label", "by label")):
        lines.append(f"  {title}:")
        for key, c in t[field_name].items():
            lines.append(f"    {key:<24} full value {c['full_value']} of {c['attempted']}; exact {c['exact']} of {c['attempted']}; "
                         f"wrong label {c['wrong_label']}; partial {c['partial']}; missed {c['missed']}")
    failures = [r for r in results if r.verdict != "exact"]
    lines += ["", "Not captured exactly:" if failures else "Every number was captured exactly."]
    for r in failures:
        e = r.expected
        lines.append(f"  {e.id} {e.value} ({e.label}, {e.style}, {e.speaker}): {VERDICT_WORDS[r.verdict]}. {r.detail}")
        if r.found:
            lines.append(f"       stored: {r.found.kind} {r.found.value!r} [{r.found.confidence}] from {r.found.quote!r} at t={r.found.t} in {r.found.call}")
    lines += ["", "Small sample: do not quote these counts as a rate. Failures above are the useful part."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Score spoken numbers against what the extractor logged (offline; staged small-sample dev data).")
    p.add_argument("expected", type=Path)
    p.add_argument("calls", nargs="+", help="call ids (folders under data/calls)")
    p.add_argument("--calls-dir", type=Path, default=VoiceConfig().calls_dir)
    p.add_argument("--json", type=Path, default=None, help="also write the results here")
    args = p.parse_args(argv)
    expected, warnings = parse_expected(args.expected)
    if not expected:
        print("The expected file has no numbers yet.")
        return 1
    unknown = sorted({e.call for e in expected if e.call and e.call not in args.calls})
    if unknown:
        print(f"Call ids used in the expected file but not given on the command line: {unknown}")
        return 1
    findings = [f for c in args.calls for f in load_findings(c, args.calls_dir)]
    results = score(expected, findings)
    print(report(results, warnings, args.calls))
    if args.json:
        args.json.write_text(json.dumps({
            "label": BANNER, "calls": args.calls, "counts": tally(results),
            "results": [{"id": r.expected.id, "expected": r.expected.value, "label": r.expected.label, "style": r.expected.style,
                         "speaker": r.expected.speaker, "verdict": r.verdict, "detail": r.detail,
                         "found": None if r.found is None else vars(r.found)} for r in results]}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
