"""Stage detection from the words of one line, with two separate test sets.

  - The INVENTED set was written by me, with the keyword list in front of me. A pass means "the rules do what I
    designed". It says nothing about real callers.
  - The OWNER sets are written by the project owner, as a scammer would talk, without looking at the keyword list:
      * stage_cases_user.txt     "OWNER set 1 (dev set, tuned, not a test)". Its untouched score (11 of 27) is in the
                                 engineering log. Keywords were then tuned with it in view, so it is FROZEN: a test
                                 below fails if its cases change, and it must never be tuned against again.
      * stage_cases_heldout.txt  reserved for the next honest measurement; do not look at the rules before writing it.
    Their result is "closer to a real caller".

Neither is an accuracy figure: all are small, and all are judged on one line at a time (its words plus what the
extractor finds in it, line_stage), not on the call history. The history rules (stickiness, allowed moves) are tested
in test_fsm.py. The owner sets are REPORTED, never asserted, so a poor score cannot fail the build.
"""
from __future__ import annotations

from pathlib import Path

import hashlib

import pytest

from mirage.config import ROOT
from mirage.fsm import STAGES, line_stage, load_rules, raw_stage

RULES = load_rules()
OWNER_FILE = ROOT / "tests" / "fixtures" / "stage_cases_user.txt"
HELDOUT_FILE = ROOT / "tests" / "fixtures" / "stage_cases_heldout.txt"
FRIEND_FILE = ROOT / "tests" / "fixtures" / "stage_cases_heldout_friend.txt"
# Set 2 lines about scams Mirage does not implement, matched by the start of the line (lower case). They are
# reported separately and left out of the in-scope score. See docs/safety.md.
OUT_OF_SCOPE_SET2 = (
    "hello ji electricity office se bol", "aaj raat bijli kat jayegi agar dues", "light chalu rakhni hai toh is number",
    "sir trai complaint ke baare mein", "aapka number band hone wala hai trai", "aapke cousin ka phone aaya tha",
    "tumhare relative ko chhudana hai",
)

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


def parse_owner_file(path: Path | None = None) -> list[tuple[int, str, str]]:
    path = path or OWNER_FILE
    cases = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        stage, sep, text = line.partition("|")
        assert sep, f"{path.name}:{number}: expected 'STAGE | text'"
        stage, text = stage.strip().upper(), text.strip()
        assert stage in STAGES, f"{path.name}:{number}: {stage!r} is not one of {STAGES}"
        assert text, f"{path.name}:{number}: empty text"
        cases.append((number, stage, text))
    return cases


def write_report(request, title: str, note: str, rows: list[tuple[str, str, str | None, str | None, float]],
                 out_of_scope=None, show_misses: bool = True) -> None:
    """rows: (text, wanted stage, stage claimed, weakest stage with any score, that score).
    out_of_scope: optional function text -> bool; those lines are reported separately and left out of the in-scope score.
    show_misses=False prints counts only (used for a set nobody is allowed to read yet)."""
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    right = sum(1 for _, want, got, _, _ in rows if want == got)
    weak_right = sum(1 for _, want, got, weak, _ in rows if got is None and weak == want)
    reporter.write_line("")
    reporter.write_line(f"== {title}: {right} of {len(rows)} lines matched ({note}) ==")
    if out_of_scope:
        inside = [r for r in rows if not out_of_scope(r[0])]
        outside = [r for r in rows if out_of_scope(r[0])]
        in_right = sum(1 for _, want, got, _, _ in inside if want == got)
        out_right = sum(1 for _, want, got, _, _ in outside if want == got)
        reporter.write_line(f"   in scope (digital arrest, fake agency, courier and customs, bank KYC): "
                            f"{in_right} of {len(inside)}")
        reporter.write_line(f"   out of scope (electricity, TRAI / SIM block, relative in trouble; not implemented): "
                            f"{out_right} of {len(outside)}")
    reporter.write_line(f"   {weak_right} more had the right stage as their best guess but scored below the minimum.")
    reporter.write_line("   Not an accuracy figure: a small set, one line at a time.")
    if not show_misses:
        reporter.write_line("   (misses are not printed for this set until the owner says so)")
        return
    for text, want, got, weak, weak_score in rows:
        if want != got:
            detail = f"got {got}" if got else f"got nothing (best guess {weak} at {weak_score:g})" if weak else "got nothing (no keyword)"
            tag = " [out of scope]" if out_of_scope and out_of_scope(text) else ""
            reporter.write_line(f"   miss: wanted {want}, {detail}{tag}: {text[:70]}")


def rows_for(cases: list[tuple[str | None, str]]) -> list[tuple[str, str | None, str | None, str | None, float]]:
    out = []
    for want, text in cases:
        claimed, weak, score = line_stage(RULES, text)
        out.append((text, want, claimed, weak, score))
    return out


# ---------------------------------------------------------------- invented set: "the rules do what I designed"

@pytest.mark.parametrize("expected, text", INVENTED)
def test_invented_set_rules_do_what_i_designed(expected, text):
    assert line_stage(RULES, text)[0] == expected


def test_invented_set_report(request):
    rows = rows_for(INVENTED)
    write_report(request, "INVENTED set (mine)", "the rules do what I designed", rows)
    assert all(want == got for _, want, got, _, _ in rows)


def test_a_word_listed_under_two_stages_scores_each_and_needs_company_to_win():
    from mirage.fsm import score_text
    scores, _ = score_text(RULES, "jaldi")
    assert scores["S2"] == 1.0 and scores["S4"] == 1.5                     # listed in both, weighted differently
    assert raw_stage(RULES, "jaldi") is None                               # neither reaches the minimum alone
    assert raw_stage(RULES, "jaldi karo turant") == "S4"                   # with company, the heavier stage wins


def test_a_tie_goes_to_the_later_stage_in_the_helper_as_in_the_brain():
    # INVENTED: "otp" is 2.0 in S4 and "band ho jayega" is 2.0 in S1
    assert raw_stage(RULES, "OTP batao, nahi toh account band ho jayega") == "S4"


def test_a_case_number_in_the_line_counts_as_evidence_for_s1():
    # the extractor finds a case number; the brain adds a boost for it, so the stage test must too
    assert line_stage(RULES, "Aapka naam case number 00001234 mein mention hai")[0] == "S1"
    assert raw_stage(RULES, "Aapka naam case number 00001234 mein mention hai") is None   # words alone are not enough


# ---------------------------------------------------------------- owner sets: "closer to a real caller"

def test_owner_set_files_are_well_formed():
    parse_owner_file(OWNER_FILE)
    parse_owner_file(HELDOUT_FILE)
    parse_owner_file(FRIEND_FILE)


def out_of_scope_set2(text: str) -> bool:
    return text.lower().startswith(OUT_OF_SCOPE_SET2)


@pytest.mark.parametrize("path, title, kwargs", [
    (OWNER_FILE, "OWNER set 1 (dev set, tuned, not a test; untouched score was 11 of 27, see the log)", {}),
    (HELDOUT_FILE, "OWNER set 2 (dev set after the fix; untouched score was 4 of 18)",
     {"out_of_scope": out_of_scope_set2}),
    (FRIEND_FILE, "set 3 (friend, held out)", {"show_misses": False}),
])
def test_owner_set_report(request, path, title, kwargs):
    cases = parse_owner_file(path)
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if not cases:
        if reporter:
            reporter.write_line("")
            reporter.write_line(f"== {title}: 0 lines yet; {path.name} has no cases ==")
        return
    write_report(request, title, "closer to a real caller", rows_for([(stage, text) for _, stage, text in cases]), **kwargs)
    # reported, not asserted: a low score here is information, not a failed build


def test_the_out_of_scope_lines_of_set_2_are_the_seven_described_in_the_log():
    texts = [text for _, _, text in parse_owner_file(HELDOUT_FILE)]
    assert sum(out_of_scope_set2(t) for t in texts) == len(OUT_OF_SCOPE_SET2) == 7


def test_owner_set_parser_reads_cases_and_rejects_bad_lines(tmp_path):
    good = tmp_path / "cases.txt"
    good.write_text("# a comment\n\nS3 | UPI ID bataiye\nS1 | arrest warrant hai\n", encoding="utf-8")
    assert [(s, t) for _, s, t in parse_owner_file(good)] == [("S3", "UPI ID bataiye"), ("S1", "arrest warrant hai")]
    for bad in ("S9 | something", "no separator here", "S2 |"):
        good.write_text(bad + "\n", encoding="utf-8")
        with pytest.raises(AssertionError):
            parse_owner_file(good)


# ---------------------------------------------------------------- freezing set 1, and one scoring function

SET1_SHA256 = "dc25134c78e612c75d9dc2b0653973c0baf01db3f86f032f106aa19b9614c789"
SET2_SHA256 = "6c39d80d389d14286e9f4b781eb5da09381aafeb7135ca80cae7bd9b65bab3d9"


def _cases_digest(path: Path) -> str:
    cases = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.strip().startswith("#")]
    return hashlib.sha256(chr(10).join(cases).encode("utf-8")).hexdigest()


def test_owner_set_1_is_frozen_after_tuning():
    assert _cases_digest(OWNER_FILE) == SET1_SHA256, (
        "stage_cases_user.txt (owner set 1) was tuned against and is frozen as a dev set. If you want new cases, "
        "write a new file; do not edit this one.")


def test_owner_set_2_is_frozen_after_the_fix():
    assert _cases_digest(HELDOUT_FILE) == SET2_SHA256, (
        "stage_cases_heldout.txt (owner set 2) was read and the rules were changed afterwards, so it is a frozen dev "
        "set now. Write new cases in a new file (the friend's set 3 is stage_cases_heldout_friend.txt).")


def test_the_brain_and_the_stage_test_use_the_same_scoring_function(monkeypatch):
    import mirage.fsm as fsm
    from mirage.fsm import CallBrain

    calls = []
    original = fsm.score_line

    def spy(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(fsm, "score_line", spy)
    CallBrain().hear("arrest warrant hai", 1.0)
    brain = CallBrain()
    brain.hear("arrest warrant hai", 1.0)
    brain.respond()
    assert calls == ["arrest warrant hai"]                    # the brain scored its turn with score_line
    fsm.line_stage(RULES, "UPI ID bataiye")
    assert calls[-1] == "UPI ID bataiye"                      # and so does the stage test helper


def test_a_fresh_brain_and_the_stage_helper_give_identical_scores_on_every_test_line():
    from mirage.fsm import CallBrain, line_scores
    owner = [text for _, _, text in parse_owner_file(OWNER_FILE)]
    for text in [t for _, t in INVENTED] + owner:
        brain = CallBrain()
        brain.state.turn = 1                                  # skip the opening greeting, which ignores the line
        brain.hear(text, 1.0, 2.0)
        assert brain.respond().stage.scores == line_scores(RULES, text), text


# ---------------------------------------------------------------- verb stems: a short explicit list

EXPECTED_STEMS = {"S2": ["ruk", "रुक"], "S3": ["bhej", "भेज", "jama", "जमा", "daal"], "S4": ["khol", "खोल"]}


def test_the_stem_list_is_the_short_explicit_one():
    assert {stage: [label for label, _, _ in items] for stage, items in RULES.stems.items()} == EXPECTED_STEMS


@pytest.mark.parametrize("word", ["bhejiye", "bhejo", "bhejna", "bhejni", "bhejne", "bhejiyega", "भेजिए", "भेजो"])
def test_every_politeness_form_of_a_stem_counts(word):
    from mirage.fsm import score_text
    scores, hits = score_text(RULES, f"paise {word}")
    assert any(h.startswith(("bhej*", "भेज*")) for h in hits["S3"]), hits


@pytest.mark.parametrize("stem", ["kar", "bol", "de", "bata", "ab", "रह"])
def test_stems_of_very_common_or_very_short_verbs_are_refused(tmp_path, stem):
    import json
    from mirage.config import ROOT as repo_root
    from mirage.fsm import RULES_PATH, load_rules
    data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    data["stems"]["S3"][stem] = 1.0
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load_rules(path)


def test_one_stem_alone_can_never_claim_a_stage():
    assert all(weight < RULES.min_score for items in RULES.stems.values() for _, _, weight in items)
    for line in ["bhejiye", "jama karo", "ruko", "app kholo", "daal do"]:
        assert line_stage(RULES, line)[0] is None, line


def test_stems_do_not_fire_on_ordinary_hinglish():
    from mirage.fsm import score_text
    ordinary = ["Kai baar bataya hai aapko", "Chai peeni hai kya", "Maine kaha tha", "mera naam Amit hai", "Bhai sahab kaise hain",
                "Aaj ka din kaisa hai", "मैं बैंक से बोल रहा हूँ", "आपका खाता बंद है", "कई बार बताया है"]
    for line in ordinary:
        _, hits = score_text(RULES, line)
        assert not any("*" in h for stage_hits in hits.values() for h in stage_hits), line


def test_a_known_collision_is_harmless_daal_means_lentils():
    from mirage.fsm import score_text
    scores, _ = score_text(RULES, "aaj daal chawal bana hai")
    assert scores["S3"] == 1.0 and line_stage(RULES, "aaj daal chawal bana hai")[0] is None
