"""Stage detection from the words of one line, with two separate test sets.

  - The INVENTED set was written by me, with the keyword list in front of me. A pass means "the rules do what I
    designed". It says nothing about real callers.
  - The OWNER set (tests/fixtures/stage_cases_user.txt) is written by the project owner, as a scammer would talk,
    without looking at the keyword list. Its result is "closer to a real caller".

Neither is an accuracy figure: both are small, and both are judged on the words of a single line (raw_stage), not
on the call history. The history rules (stickiness, allowed moves) are tested in test_fsm.py.
The owner set is REPORTED, never asserted, so a poor score cannot fail the build; it is a measurement.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mirage.config import ROOT
from mirage.fsm import STAGES, load_rules, raw_stage

RULES = load_rules()
OWNER_FILE = ROOT / "tests" / "fixtures" / "stage_cases_user.txt"

# (expected stage or None, line). None means "below the minimum score: no stage is claimed".
INVENTED = [
    ("S0", "hello sir namaste main FedEx courier se bol raha hoon"),
    ("S0", "aapke naam se ek parcel aaya hai"),
    ("S0", "main bank se bol raha hoon aapka KYC update karna hai"),
    ("S0", "customs se parcel ke baare mein call hai"),
    ("S1", "aapke khilaf arrest warrant nikla hai"),
    ("S1", "CBI officer ne aapka case register kiya hai"),
    ("S1", "ye money laundering ka case hai, police aapko giraftar karegi"),
    ("S1", "aapka account band ho jayega agar aapne KYC update nahi kiya"),
    ("S1", "narcotics ka case hai aur FIR darj ho chuki hai"),
    ("S1", "court mein pesh hona padega, legal action hoga"),
    ("S1", "senior officer se baat kijiye"),
    ("S1", "aap jail jaa sakte hain"),
    ("S2", "hello hello aap kahan gaye, kitna time lagega"),
    ("S2", "line par rahiye, jaldi dhoondhiye paper"),
    ("S2", "kab tak intezaar karwayenge, hold kar raha hoon"),
    ("S2", "aap sun rahe hain na, kahan ho"),
    ("S3", "ab payment karna hoga, UPI ID bataiye"),
    ("S3", "RBI ke safe account mein paise transfer kijiye"),
    ("S3", "account number aur IFSC code likhiye"),
    ("S3", "verification amount jama karna hoga"),
    ("S3", "is QR code ko scan karke gpay se bhejiye"),
    ("S3", "aapko do lakh rupaye bhejne honge"),
    ("S3", "पेमेंट यूपीआई से कीजिए"),
    ("S4", "jaldi karo turant, der mat karo warna problem hogi"),
    ("S4", "anydesk install karo abhi"),
    ("S4", "OTP batao jaldi"),
    ("S4", "screen share karo aur link par click karo"),
    ("S4", "abhi tak nahi hua, kya hua, payment kyun nahi ho raha"),
    ("S4", "last chance hai, time nahi hai"),
    ("S5", "theek hai baad mein baat karte hain, bye"),
    ("S5", "time waste mat karo, main phone rakh raha hoon"),
    ("S5", "faltu mein time barbaad, bewakoof"),
    ("S5", "call kaat raha hoon, goodbye"),
    # Lines with nothing to go on, and single words that appear under two stages (jaldi: S2 and S4): no stage claimed.
    (None, "haan ji bataiye"),
    (None, "kya aap sun sakte hain"),
    (None, "okay"),
    (None, "mera naam Amit hai"),
    (None, "theek hai"),
    (None, "jaldi"),
    (None, "account"),
    (None, "case"),
]


def parse_owner_file() -> list[tuple[int, str, str]]:
    cases = []
    for number, raw in enumerate(OWNER_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        stage, sep, text = line.partition("|")
        assert sep, f"{OWNER_FILE.name}:{number}: expected 'STAGE | text'"
        stage, text = stage.strip().upper(), text.strip()
        assert stage in STAGES, f"{OWNER_FILE.name}:{number}: {stage!r} is not one of {STAGES}"
        assert text, f"{OWNER_FILE.name}:{number}: empty text"
        cases.append((number, stage, text))
    return cases


def write_report(request, title: str, note: str, rows: list[tuple[str, str | None, str | None]]) -> None:
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    right = sum(1 for _, want, got in rows if want == got)
    reporter.write_line("")
    reporter.write_line(f"== {title}: {right} of {len(rows)} lines matched ({note}) ==")
    reporter.write_line("   Not an accuracy figure: a small set judged on the words of single lines.")
    for text, want, got in rows:
        if want != got:
            reporter.write_line(f"   miss: wanted {want}, got {got}: {text[:70]}")


# ---------------------------------------------------------------- invented set: "the rules do what I designed"

@pytest.mark.parametrize("expected, text", INVENTED)
def test_invented_set_rules_do_what_i_designed(expected, text):
    assert raw_stage(RULES, text) == expected


def test_invented_set_report(request):
    rows = [(text, want, raw_stage(RULES, text)) for want, text in INVENTED]
    write_report(request, "INVENTED set (mine)", "the rules do what I designed", rows)
    assert all(want == got for _, want, got in rows)


def test_a_word_listed_under_two_stages_scores_each_and_needs_company_to_win():
    from mirage.fsm import score_text
    scores, _ = score_text(RULES, "jaldi")
    assert scores["S2"] == 1.0 and scores["S4"] == 1.5                     # listed in both, weighted differently
    assert raw_stage(RULES, "jaldi") is None                               # neither reaches the minimum alone
    assert raw_stage(RULES, "jaldi karo turant") == "S4"                   # with company, the heavier stage wins


# ---------------------------------------------------------------- owner set: "closer to a real caller"

def test_owner_set_file_is_well_formed():
    parse_owner_file()


def test_owner_set_report(request):
    cases = parse_owner_file()
    rows = [(text, stage, raw_stage(RULES, text)) for _, stage, text in cases]
    if not rows:
        reporter = request.config.pluginmanager.get_plugin("terminalreporter")
        if reporter:
            reporter.write_line("")
            reporter.write_line("== OWNER set (yours): 0 lines yet; tests/fixtures/stage_cases_user.txt has no cases ==")
        return
    write_report(request, "OWNER set (yours)", "closer to a real caller", rows)
    # reported, not asserted: a low score here is information, not a failed build


def test_owner_set_parser_reads_cases_and_rejects_bad_lines(tmp_path, monkeypatch):
    good = tmp_path / "cases.txt"
    good.write_text("# a comment\n\nS3 | UPI ID bataiye\nS1 | arrest warrant hai\n", encoding="utf-8")
    monkeypatch.setattr("test_stage_detection.OWNER_FILE", good)
    assert [(s, t) for _, s, t in parse_owner_file()] == [("S3", "UPI ID bataiye"), ("S1", "arrest warrant hai")]
    for bad in ("S9 | something", "no separator here", "S2 |"):
        good.write_text(bad + "\n", encoding="utf-8")
        with pytest.raises(AssertionError):
            parse_owner_file()
