"""Tests taken from the owner's rehearsal call of 2026-11-03 (id 20261103_162757_6dd0), STAGED development data, not an evaluation.
The texts below are the transcripts as logged in data/calls/. Nothing touches the network."""
from __future__ import annotations

from mirage.config import FSMConfig, KNOWN_UPI_PROVIDERS
from mirage.extract import Finding, Transcript, extract_intel, inside_vpa, tidy_findings
from mirage.fsm import CallBrain
from mirage.live import finding_dict

# (arrival time, seconds of audio, text) in the order logged; the groups are the turns the brain answered together.
CALL_TURNS = [
    [(55.906, 7.956, "मेरा direct number लिख लीजिए। phone number है 0000"), (58.25, 2.496, "तीन एक आठ।"), (60.531, 2.24, "चार, पाँच, दो।")],
    [(88.109, 4.276, "UPI ID है verify officer"), (89.953, 1.824, "One two three"), (91.812, 1.856, "At okay, at sir.")],
    [(100.718, 2.228, "UPI ID है।"), (106.343, 5.536, "Verify officer one two three at okay access")],
    [(120.218, 3.988, "Verify officer one two three"), (121.421, 1.28, "At"), (123.062, 1.696, "Okay"), (125.046, 1.984, "AXIX")],
]


def replay(turns=CALL_TURNS) -> CallBrain:
    brain = CallBrain(config=FSMConfig(synthetic_mode=True), seed=1)
    for group in turns:
        for t, audio_s, text in group:
            brain.hear(text, t, audio_s=audio_s)
        brain.respond()
    return brain


def ids(brain: CallBrain) -> list[Finding]:
    return brain.state.intel.get("vpa", [])


# ---------------------------------------------------------------- 1. the same UPI id in another case is one card

def test_the_same_upi_id_in_a_different_case_is_kept_once_with_the_first_spelling_and_a_note():
    turns = [[(88.109, 4.276, "UPI ID है verify officer"), (89.953, 1.824, "One two three"), (91.812, 1.856, "At okay, at sir.")],
             [(100.718, 2.228, "UPI ID है।"), (106.343, 5.536, "Verify officer one two three at okay")]]    # the rehearsal's words, "access" left out
    vpas = ids(replay(turns))
    assert len(vpas) == 1
    assert vpas[0].value == "verifyofficer123@okay"                      # first spelling, as heard
    assert "said again at 106.3 s as 'Verifyofficer123@okay'" in vpas[0].reason


def test_the_rehearsal_call_now_has_a_second_card_for_okay_access_read_as_okaxis():
    """Since the "ok axis" join, the second mention ("at okay access", one piece) is read as @okaxis, a different handle from the first
    ("at okay"), so the two are not merged: they differ in the handle, not in case. Both stay unconfirmed."""
    first, second = ids(replay())
    assert first.value == "verifyofficer123@okay" and first.needs_confirmation
    assert second.value == "Verifyofficer123@okaxis" and second.needs_confirmation and second.confidence == "heard"
    assert second.quote == "Verify officer one two three at okay access"


def test_extract_and_tidy_agree_when_both_spellings_are_in_one_list():
    found = extract_intel([Transcript("UPI ID है verify officer one two three at okay", 0.0),
                           Transcript("UPI ID है Verify officer one two three at okay", 20.0)], synthetic_mode=True)
    vpas = [f for f in found if f.kind == "vpa"]
    assert [f.value for f in vpas] == ["verifyofficer123@okay"] and "said again" in vpas[0].reason


def test_a_plain_repeat_is_still_silent_and_two_different_ids_are_both_kept():
    a = Finding("vpa", "rajesh@ybl", True, "", "heard", False, "q", 1.0, 0)
    assert tidy_findings([a, a]) == [a]
    b = Finding("vpa", "suresh@ybl", True, "", "heard", False, "q2", 2.0, 1)
    assert len(tidy_findings([a, b])) == 2
    # case-insensitive only for UPI ids: two numbers or letters are not touched
    n1 = Finding("letters", "ABC", True, "", "heard", True, "q", 1.0, 0)
    n2 = Finding("letters", "abc", True, "", "heard", True, "q", 2.0, 1)
    assert len(tidy_findings([n1, n2])) == 2


# ---------------------------------------------------------------- 2. the digits of a stored UPI id are not a card of their own

def test_the_leftover_123_inside_a_stored_upi_quote_no_longer_becomes_its_own_card():
    brain = replay()
    assert "ambiguous" not in brain.state.intel                          # the logged call had a card "123" at 120.2 s


def test_a_digit_run_elsewhere_is_still_kept():
    vpa = Finding("vpa", "verifyofficer123@okay", True, "", "heard", True, "verify officer | One two three | At okay", 88.1, 0)
    same = Finding("ambiguous", "123", True, "", "none", True, "one two three", 120.2, 4)
    other_words = Finding("ambiguous", "123", True, "", "none", True, "teen sau bees", 120.2, 4)
    other_digits = Finding("ambiguous", "456", True, "", "none", True, "one two three", 120.2, 4)
    labelled = Finding("case", "123", True, "", "keyword", False, "one two three", 120.2, 4)
    assert inside_vpa(same, [vpa])
    assert not inside_vpa(other_words, [vpa]) and not inside_vpa(other_digits, [vpa]) and not inside_vpa(labelled, [vpa])
    assert tidy_findings([vpa, other_words]) == [vpa, other_words]


# ---------------------------------------------------------------- 3. "okay" for "ok axis": keep the heard value, suggest handles

def test_an_unknown_ok_handle_keeps_the_heard_value_needs_confirmation_and_lists_likely_handles():
    f = ids(replay())[0]
    assert f.value == "verifyofficer123@okay" and f.needs_confirmation and f.confidence == "heard"
    assert f.suggested == "verifyofficer123@okaxis"                      # a guess, kept apart from the value
    assert f.candidates[0] == "verifyofficer123@okaxis" and set(f.candidates) == {
        f"verifyofficer123@{h}" for h in KNOWN_UPI_PROVIDERS if h.startswith("ok")}


def test_known_handles_and_non_ok_handles_get_no_near_miss_list():
    for text in ("UPI ID है rajesh dot test at oksbi", "UPI ID है rajesh dot test at ybl", "UPI ID है rajesh dot test at gmail"):
        f = extract_intel([Transcript(text, 0.0)])[0]
        assert f.kind == "vpa" and f.candidates == () and f.suggested is None


def test_an_ok_word_that_is_not_a_handle_is_never_swapped_for_one():
    f = extract_intel([Transcript("UPI ID है rajesh dot test at okidoki", 0.0)])[0]
    assert f.value == "rajesh.test@okidoki" and f.needs_confirmation
    assert f.candidates and all(c.startswith("rajesh.test@ok") for c in f.candidates)   # a list of guesses is allowed; the value is not changed


def test_the_dashboard_message_carries_the_candidates():
    d = finding_dict(ids(replay())[0])
    assert d["value"] == "verifyofficer123@okay" and d["suggested"] == "verifyofficer123@okaxis"
    assert d["candidates"][0] == "verifyofficer123@okaxis"


def test_the_handle_list_is_marked_unverified_in_the_code():
    import mirage.config as cfg
    src = open(cfg.__file__, encoding="utf8").read()
    assert "UNVERIFIED" in src[src.index("KNOWN_UPI_PROVIDERS") - 400: src.index("KNOWN_UPI_PROVIDERS")]


# ---------------------------------------------------------------- 4. the phone number: stored as Prisma wrote it

def test_the_phone_number_is_stored_exactly_as_the_logged_text_reads():
    """Failure gallery candidate, NOT part of the E1 count. The caller says the number ends 3 1 8 6 5 2; Prisma's text reads
    "तीन एक आठ | चार, पाँच, दो" = 3 1 8 4 5 2. The extractor stored what the text says, with `keyword` confidence and no warning."""
    f = replay().state.intel["phone"][0]
    assert f.value == "0000318452" and f.confidence == "keyword" and not f.needs_confirmation
    assert "चार" in f.quote and "छह" not in f.quote and "छः" not in f.quote
