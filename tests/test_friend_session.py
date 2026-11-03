"""Tests taken from the friend session of 2026-11-02/03: five calls where a friend role-played the caller (STAGED, development
data, not an evaluation). The sentences below are the friend's transcripts as saved in data/calls/*; what Uncle ought to do with
them is decided here. Nothing touches the network."""
from __future__ import annotations

import pytest

from mirage.config import FSMConfig
from mirage.extract import Transcript, extract_intel, lone_digits, trailing_label
from mirage.fsm import CallBrain, detect_signals, load_rules, raw_stage, score_text_ex

RULES = load_rules()


def at_stage(stage: str) -> CallBrain:
    brain = CallBrain(config=FSMConfig(synthetic_mode=True))
    brain.state.stage, brain.state.turn = stage, 1
    return brain


def say(brain: CallBrain, line: str, gap: float = 6.0):
    last = brain.transcripts[-1].t if brain.transcripts else 0.0
    brain.hear(line, last + gap, audio_s=2.0)
    return brain.respond()


# ---------------------------------------------------------------- negation: a warning is not a payment demand

CALL2_WARNING = "Sir कुछ callers आपको instant KYC के नाम पर Payment करने को बोल सकते हैं। आप ऐसे मत कर करिएगा।"
CALL4_DEMAND = "सब मैं समझ रहा हूँ लेकिन system में payment reflect नहीं हुआ।"


def test_call_2_a_warning_not_to_pay_no_longer_moves_the_call_to_s3():
    scores, hits, ignored = score_text_ex(RULES, CALL2_WARNING)
    assert scores["S3"] == 0 and not hits["S3"] and any("ayment" in i for i in ignored["S3"])
    turn = say(at_stage("S0"), CALL2_WARNING)
    assert turn.stage.stage != "S3"
    assert any("ignored" in r for r in turn.stage.reasons)            # and the log says why


def test_call_4_a_real_demand_with_a_nahin_that_belongs_to_another_verb_still_counts():
    scores, hits, _ = score_text_ex(RULES, CALL4_DEMAND)
    assert hits["S3"] == ["payment +3"]
    assert say(at_stage("S0"), CALL4_DEMAND).stage.stage == "S3"


@pytest.mark.parametrize("line", [
    "आपको बिना समझे कोई payment नहीं करनी चाहिए।",                                       # call 1, turn 16
    "जी sir, KYC update के लिए किसी unknown UPI ID पर पैसे transfer करने की जरूरत नहीं होनी चाहिए।",   # call 2, turn 13
    "बिल्कुल sir official police verification में किसी unknown account तो payment करने की जरूरत नहीं है।",   # call 3, turn 15
])
def test_other_warnings_from_the_friend_session_do_not_count_as_payment_words(line):
    scores, _, ignored = score_text_ex(RULES, line)
    assert scores["S3"] == 0 and ignored["S3"]


@pytest.mark.parametrize("line", [
    "सर बस एक request है अपना OTP पिन या password किसी को मत बताएँ।",                       # call 2, turn 14
    "बिल्कुल नहीं। OTP, OTP, PIN या bank details किसी को मत दीजिए।",                        # call 3, turn 17
])
def test_a_warning_about_a_code_is_not_a_request_for_a_code(line):
    names = detect_signals(RULES, line).names
    assert "ask_otp" not in names and "ask_credential" not in names
    assert score_text_ex(RULES, line)[0]["S4"] == 0


@pytest.mark.parametrize("line, signal, stage", [
    ("अपना OTP बताइए अभी", "ask_otp", "S4"),
    ("आप OTP bataiye", "ask_otp", "S4"),
    ("payment कर दीजिए अभी", None, "S3"),
    ("Payment नहीं करेंगे तो arrest हो जाएगा", None, "S3"),            # a threat: the "nahin" is not next to a request verb
    ("Payment कीजिए, tension मत लीजिए", None, "S3"),                   # "mat" belongs to a different verb
])
def test_a_real_request_still_counts(line, signal, stage):
    assert score_text_ex(RULES, line)[0][stage] > 0
    if signal:
        assert signal in detect_signals(RULES, line).names


# ---------------------------------------------------------------- the digit wait is only for text that ends on digits

def test_the_long_digit_wait_applies_only_when_the_text_ends_on_a_digit():
    brain = at_stage("S1")
    say(brain, "case number hai chaar saat do nau ek")            # Uncle will read back and ask for the rest
    assert brain.state.awaiting_digits
    brain.hear("Sir, आपको घबराने की ज़रूरत नहीं है। पहले बात हम समझें।", 30.0, 2.0)   # call 3, turn 9: no digits
    assert brain.reply_wait_s() == FSMConfig().normal_wait_s and not brain.in_digit_mode()
    brain.hear("दो नौ", 31.0, 1.0)
    assert brain.reply_wait_s() == FSMConfig().digit_wait_s and brain.in_digit_mode()


# ---------------------------------------------------------------- the caller says they are hanging up

@pytest.mark.parametrize("line", [
    "ठीक है sir, अगर आप अभी बात नहीं करना चाहते तो मैं call end करता हूँ।",     # call 1
    "ठीक है sir आप branch में verify कर लीजिए। मैं call close करता हूँ।",         # call 2
    "मैं कॉल एंड करता हूँ",
    "ठीक है, कॉल क्लोज कर रहा हूँ",
])
def test_call_end_and_call_close_are_goodbye_words(line):
    assert raw_stage(RULES, line) == "S5"
    turn = say(at_stage("S1"), line)
    assert turn.stage.stage == "S5" and turn.plan.tactic == "goodbye_wrap"


def test_call_is_not_a_goodbye_word_by_itself():
    assert raw_stage(RULES, "sir main call kar raha hoon verification ke liye") != "S5"


# ---------------------------------------------------------------- label and digits across adjacent turns

def test_a_label_at_the_end_of_one_turn_labels_the_number_in_the_next():
    assert trailing_label("सब मैं समझ सकता हूँ लेकिन system में आपका नाम आ रहा है। Case reference") == "case"
    assert trailing_label("Case reference 0000 एक दो तीन चार है") is None            # the number came in the same turn
    brain = at_stage("S1")
    say(brain, "सब मैं समझ सकता हूँ लेकिन system में आपका नाम आ रहा है। Case reference")
    turn = say(brain, "0000 एक, दो, तीन, चार है।")
    (f,) = turn.findings
    assert (f.kind, f.value, f.confidence) == ("case", "00001234", "expected") and f.needs_confirmation   # labelled, but flagged
    control = say(at_stage("S1"), "0000 एक, दो, तीन, चार है।").findings[0]                 # no label before: not a case card
    assert control.kind == "ambiguous"


def test_the_label_is_carried_to_the_next_turn_only():
    brain = at_stage("S1")
    say(brain, "Case reference")
    say(brain, "ek minute sir")
    assert say(brain, "0000 एक, दो, तीन, चार है।").findings[0].kind == "ambiguous"


def test_a_repeat_of_the_end_of_a_number_we_hold_is_not_a_second_card():
    # call 1, turns 7 and 8: "Case reference 0000 एक, दो, तीन, चार है" (kept), then "एक, दो, तीन, चार।" labelled only by Uncle's hint
    brain = at_stage("S1")
    first = say(brain, "system में आपका नाम आ रहा है। Case reference 0000 एक, दो, तीन, चार है।")
    assert [f.value for f in first.findings] == ["00001234"] and first.findings[0].confidence == "keyword"
    second = say(brain, "एक, दो, तीन, चार।")
    assert second.findings == ()
    assert [f.value for f in brain.state.intel["case"]] == ["00001234"]


def test_the_same_list_never_holds_a_keyword_number_and_a_hinted_piece_of_it():
    found = extract_intel([Transcript("Case reference 0000 एक, दो, तीन, चार है", 1.0, 3.0),
                           Transcript("एक, दो, तीन, चार।", 9.0, 2.0)], expected="case")
    assert [f.value for f in found] == ["00001234"]


def test_a_different_number_of_the_same_kind_is_still_kept():
    found = extract_intel([Transcript("Case reference 0000 एक, दो, तीन, चार है", 1.0, 3.0),
                           Transcript("नहीं sir, एक, दो, तीन, पाँच।", 9.0, 2.0)], expected="case")
    assert {f.value for f in found} == {"00001234", "1235"}


def test_a_lone_digit_after_a_read_back_completes_the_number_it_continues():
    # call 1, turn 9: Uncle read back "1 2 3" and asked for the rest; the caller said just "चार।" and it was thrown away
    assert lone_digits("चार।") == "4" and lone_digits("एक, दो, तीन, चार।") == "" and lone_digits("चार लोग") == ""
    brain = at_stage("S1")
    brain.state.expected = "case"
    first = say(brain, "एक, दो, तीन, चार।")
    (f,) = first.findings
    assert (f.value, f.confidence, f.needs_confirmation) == ("1234", "expected", True)
    assert first.plan.tactic == "readback_partial" and brain.state.readback_open == ("case", "1234", 3)
    second = say(brain, "चार।")
    assert second.findings == ()                                   # not a new number ...
    (held,) = brain.state.intel["case"]
    assert held.value == "1234" and not held.needs_confirmation    # ... but the end of the one Uncle read back, so it is confirmed


def test_a_lone_digit_that_does_not_match_changes_nothing():
    brain = at_stage("S1")
    brain.state.expected = "case"
    say(brain, "एक, दो, तीन, चार।")
    say(brain, "सात।")
    (held,) = brain.state.intel["case"]
    assert held.value == "1234" and held.needs_confirmation


def test_a_lone_digit_without_a_read_back_before_it_changes_nothing():
    brain = at_stage("S1")
    turn = say(brain, "चार।")
    assert turn.findings == () and brain.state.intel == {}
