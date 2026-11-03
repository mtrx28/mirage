"""The scam score (real evidence only) and the exit rule, tested on the friend's 10 scripts (tests/fixtures/friend_scripts.txt).

DEV DATA FROM ONE FRIEND, NOT A TEST OF ACCURACY. The 10 scripts (5 scam, 5 ordinary) were in view when the evidence list was written,
so a pass here means "the rules do what they were written to do on these scripts", nothing more. The sealed red-team lines are the
real check. Only the caller's lines are used; Uncle's lines in the file are the friend's guess and are ignored. Nothing here touches
the network.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from mirage.config import FSMConfig, GateConfig
from mirage.fsm import CallBrain, load_rules, score_text_ex
from mirage.lines import ASKS_FOR_CREDENTIALS
from mirage.negation import NO_NEGATION
from mirage.safety import (Gate, GateLine, evidence_in, lint_gate_lines, load_evidence_rules, load_gate_lines, scam_score,
                           scam_score_parts)

SCRIPTS_FILE = Path(__file__).parent / "fixtures" / "friend_scripts.txt"
CFG = GateConfig()
RULES = load_rules()
ER = load_evidence_rules()
NEUTRAL = ["Haan ji.", "Achha.", "Theek hai.", "Okay sir.", "Haan bilkul.", "Ji sir.", "Achha theek hai.", "Haan haan.",
           "Theek hai ji.", "Okay.", "Haan.", "Achha ji."]


def load_scripts() -> list[dict]:
    scripts, cur = [], None
    for line in SCRIPTS_FILE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(\d+)\.\s+(.*)$", line.strip())
        if m and not line.startswith(("Caller", "Uncle")):
            cur = {"n": int(m.group(1)), "title": m.group(2), "lines": []}
            scripts.append(cur)
        elif cur and line.startswith("Caller:"):
            cur["lines"].append(line[len("Caller:"):].strip())
    return scripts


SCRIPTS = load_scripts()
SCAM = [s for s in SCRIPTS if s["n"] <= 5]
BENIGN = [s for s in SCRIPTS if s["n"] > 5]


def new_brain(cfg: GateConfig = CFG) -> CallBrain:
    return CallBrain(config=FSMConfig(synthetic_mode=True), gate=Gate(cfg), seed=0)


def play(brain: CallBrain, lines: list[str]):
    """Feed each line as its own turn; return [(score after the turn, the turn)]."""
    out, t = [], 0.0
    for line in lines:
        t += 6.0
        brain.hear(line, t, 2.0)
        turn = brain.respond()
        out.append((scam_score(brain.state, cfg_of(brain)), turn))
        if brain.state.ended:
            break
    return out


def cfg_of(brain: CallBrain) -> GateConfig:
    return brain.gate.cfg


def actions(brain: CallBrain) -> list[str]:
    return [g["action"] for g in brain.state.gate_log]


# ---------------------------------------------------------------- the fixture itself

def test_the_friend_scripts_are_five_scam_and_five_ordinary_calls():
    assert [s["n"] for s in SCRIPTS] == list(range(1, 11))
    assert len(SCAM) == 5 and len(BENIGN) == 5 and all(s["lines"] for s in SCRIPTS)


# ---------------------------------------------------------------- the score: scam scripts high, ordinary ones at zero (dev data)

@pytest.mark.parametrize("script", SCAM, ids=lambda s: f"scam{s['n']}")
def test_a_scam_script_never_makes_uncle_leave_and_its_score_only_grows(script):
    brain = new_brain()
    rows = play(brain, script["lines"])
    scores = [r[0] for r in rows]
    assert scores == sorted(scores)
    assert "exit_polite" not in actions(brain) and not brain.state.ended


@pytest.mark.parametrize("script", SCAM, ids=lambda s: f"scam{s['n']}")
def test_a_scam_script_is_over_the_exit_line_by_the_first_turn_an_exit_is_allowed(script):
    # padded with neutral talk, the scam must still be over the line at caller turn grace+1 and never exit
    brain = new_brain()
    rows = play(brain, script["lines"] + NEUTRAL)
    assert len(rows) == len(script["lines"]) + len(NEUTRAL)                 # nothing ended the call
    first_eligible = CFG.exit_grace_turns + 1
    assert rows[first_eligible - 1][0] >= CFG.exit_score_below + 1.0         # at least 1 point of margin, on these scripts
    assert "exit_polite" not in actions(brain)


@pytest.mark.parametrize("script", BENIGN, ids=lambda s: f"benign{s['n']}")
def test_an_ordinary_call_scores_zero_on_these_scripts(script):
    brain = new_brain()
    rows = play(brain, script["lines"])
    assert [r[0] for r in rows] == [0.0] * len(script["lines"])
    assert set(actions(brain)) == {"pass"}                                   # also: not a single gate replacement


# ---------------------------------------------------------------- the exit rule

@pytest.mark.parametrize("script", BENIGN, ids=lambda s: f"benign{s['n']}")
def test_an_ordinary_caller_who_keeps_talking_gets_a_polite_honest_exit_right_after_the_grace_period(script):
    brain = new_brain()
    rows = play(brain, script["lines"] + NEUTRAL)
    plan = rows[-1][1].plan
    assert len(rows) == CFG.exit_grace_turns + 1                              # not a turn earlier, not a turn later
    assert plan.gate_action == "exit" and plan.end_call and brain.state.ended and plan.gated
    assert "automated assistant" in plan.text and re.search(r"shukriya|maaf", plan.text, re.I)
    assert not re.search(r"\bmain (ek )?(insaan|human|person)\b", plan.text, re.I)
    assert actions(brain)[:-1] == ["pass"] * CFG.exit_grace_turns
    assert "grace period" in brain.state.gate_log[-1]["why"]


def test_nothing_is_said_after_the_exit():
    brain = new_brain()
    rows = play(brain, NEUTRAL)
    assert brain.state.ended and rows[-1][1].plan.gate_action == "exit"
    brain.hear("Hello? Are you there?", 999.0, 1.0)
    after = brain.respond()
    assert after.plan.text == "" and after.plan.gate_action == "call_over"


def test_no_exit_inside_the_grace_period_whatever_the_score():
    brain = new_brain()
    rows = play(brain, NEUTRAL[:CFG.exit_grace_turns])
    assert len(rows) == CFG.exit_grace_turns and not brain.state.ended and set(actions(brain)) == {"pass"}


def test_the_grace_period_threshold_and_switch_are_config_values():
    short = GateConfig(exit_grace_turns=2)
    brain = new_brain(short)
    assert len(play(brain, NEUTRAL)) == 3 and brain.state.gate_log[-1]["action"] == "exit_polite"
    off = new_brain(GateConfig(exit_enabled=False))
    assert len(play(off, NEUTRAL)) == len(NEUTRAL) and not off.state.ended
    strict = new_brain(GateConfig(exit_grace_turns=2, exit_score_below=0.0))      # a score is never below 0: never exits
    assert len(play(strict, NEUTRAL)) == len(NEUTRAL)


def test_one_piece_of_real_evidence_keeps_a_caller_from_being_exited_at_the_default_line():
    # the line is "under 1.0"; a single authority claim is 1.0: borderline, and on the staying side
    brain = new_brain()
    rows = play(brain, ["Main customs department se bol raha hoon."] + NEUTRAL)
    assert rows[0][0] == 1.0 and len(rows) == len(NEUTRAL) + 1 and not brain.state.ended


# ---------------------------------------------------------------- the paths already built still win over the exit

def after_grace_benign() -> CallBrain:
    brain = new_brain()
    play(brain, NEUTRAL[:CFG.exit_grace_turns])
    return brain


def test_an_identity_question_after_the_grace_period_is_answered_by_the_identity_path_not_the_exit():
    brain = after_grace_benign()
    plan = play(brain, ["are you a robot?"])[0][1].plan
    assert plan.gate_action == "disclose" and "automated assistant" in plan.text and brain.state.gate_log[-1]["action"] == "disclose_identity"


def test_an_emergency_after_the_grace_period_is_answered_by_the_emergency_path_not_the_exit():
    brain = after_grace_benign()
    plan = play(brain, ["mere pitaji ko heart attack aaya hai"])[0][1].plan
    assert brain.state.gate_log[-1]["action"] == "disclose_emergency" and "112" in plan.text.replace(" ", "").replace(",", "")


def test_a_hostile_caller_after_the_grace_period_still_gets_the_calm_answers_first():
    brain = after_grace_benign()
    plan = play(brain, ["tum bewakoof ho"])[0][1].plan
    assert plan.gate_action == "hostile_calm" and not plan.end_call and not brain.state.ended


def test_a_goodbye_the_brain_already_chose_is_not_replaced_by_the_exit():
    brain = after_grace_benign()
    plan = play(brain, ["theek hai sir, call disconnect karta hoon, bye"])[0][1].plan
    assert plan.tactic == "goodbye_wrap" and plan.gate_action != "exit"


# ---------------------------------------------------------------- the evidence: real asks count, warnings and bare words do not

@pytest.mark.parametrize("line, category", [
    ("Aapke Aadhaar details confirm kar dijiye.", "credential_ask"),
    ("Aapke registered mobile pe OTP aayega.", "credential_ask"),
    ("Aapko abhi UPI se payment karna hoga.", "payment_demand"),
    ("Main CBI se Inspector Sharma bol raha hoon.", "authority"),
    ("Aapke against arrest warrant issue ho sakta hai.", "threat"),
    ("Nahi toh legal action ho sakta hai, immediately karo.", "pressure"),
    ("Kisi ko bhi iske baare mein mat batana.", "secrecy"),
    ("Main aapko ek link bhej deta hoon.", "install_or_link"),
])
def test_real_evidence_counts(line, category):
    assert category in evidence_in(line, ER, RULES.negation)


@pytest.mark.parametrize("line", [
    "Aapke Aadhaar ke against ek serious complaint register hui hai.",     # script 3: Aadhaar is mentioned, nothing is asked for
    "Main abhi railway station pe hoon.",                                   # script 7: a bare "abhi"
    "Main Suresh! Itni jaldi bhool gaye?",                                  # script 10: a bare "jaldi"
    "Aap maturity ke baad amount savings account mein credit karwana chahenge ya FD renew karna chahenge?",   # script 9: amount, account
    "Aapka parcel deliver karna tha.",                                      # script 8
    "Case police ko transfer ho jayega.",                                   # a bare "transfer" is not a payment word
    "Arre, ye 00001234 number nahi hai kya?",                               # script 6: a number alone is not evidence
])
def test_ordinary_words_and_bare_mentions_do_not_count(line):
    found = evidence_in(line, ER, RULES.negation)
    assert "credential_ask" not in found and "payment_demand" not in found and "pressure" not in found
    assert "secrecy" not in found and "install_or_link" not in found


@pytest.mark.parametrize("line", [
    "आपको बिना समझे कोई payment नहीं करनी चाहिए।",                                   # call 1 (staged), a warning
    "Payment करने को बोल सकते हैं। आप ऐसे मत कर करिएगा।",                               # call 2
    "सर बस एक request है अपना OTP पिन या password किसी को मत बताएँ।",                   # call 2
    "बिल्कुल नहीं। OTP, OTP, PIN या bank details किसी को मत दीजिए।",                    # call 3
    "तो payment करने की जरूरत नहीं है।",                                              # call 3
])
def test_a_warning_against_payment_or_a_code_is_not_evidence_of_a_scam(line):
    found = evidence_in(line, ER, RULES.negation)
    assert "payment_demand" not in found and "credential_ask" not in found
    assert set(evidence_in(line, ER, NO_NEGATION)) & {"payment_demand", "credential_ask"}        # and without the fix it would be


def test_a_demand_with_a_nahin_that_belongs_to_another_verb_still_counts():
    assert "payment_demand" in evidence_in("Sir, woh payment reflect nahi hua hai.", ER, RULES.negation)       # script 4
    assert "payment_demand" in evidence_in("सब मैं समझ रहा हूँ लेकिन system में payment reflect नहीं हुआ।", ER, RULES.negation)   # call 4


# ---------------------------------------------------------------- a threat worded with a request verb ("payment nahin karna toh arrest")
# Until the red-team step this was a KNOWN LIMIT (the payment word was read as a warning). A negated payment followed by "toh" and a bad
# consequence is now a threat and counts; a warning with a good or denied consequence is still a warning.

@pytest.mark.parametrize("threat", [
    "Payment nahin karna toh arrest ho jayega.",
    "UPI se paise nahin bhejna toh bahut bura hoga.",
    "Payment mat karo toh warrant nikal jayega.",
    "Agar payment nahin karni toh account block ho jayega.",
    "Payment nahin karna padega toh police aa jayegi.",
])
def test_a_negated_payment_followed_by_toh_and_a_bad_consequence_is_a_threat_and_counts(threat):
    scores, hits, ignored = score_text_ex(RULES, threat)
    assert "payment_demand" in evidence_in(threat, ER, RULES.negation)
    assert scores["S3"] > 0 and not ignored["S3"]


@pytest.mark.parametrize("warning", [
    "Payment nahin karna chahiye toh aap safe rahenge.",             # a good consequence
    "Payment nahin karna toh koi problem nahin hogi.",               # the consequence is denied
    "Payment karne ki zaroorat nahin hai toh aap befikar rahiye.",
    "आपको बिना समझे कोई payment नहीं करनी चाहिए।",                       # call 1
    "Payment करने को बोल सकते हैं। आप ऐसे मत कर करिएगा।",                 # call 2
    "तो payment करने की जरूरत नहीं है।",                                 # call 3
])
def test_a_warning_with_no_bad_consequence_is_still_a_warning(warning):
    assert "payment_demand" not in evidence_in(warning, ER, RULES.negation)
    assert score_text_ex(RULES, warning)[0]["S3"] == 0


def test_the_threat_reading_does_not_turn_a_negated_payment_into_a_demand_without_toh():
    assert "payment_demand" not in evidence_in("Payment nahin karna, arrest ho jayega.", ER, RULES.negation)    # no "toh": left as before


@pytest.mark.parametrize("threat", [
    "Agar payment nahin kiya toh arrest ho jayega.",
    "Payment nahin karenge toh warrant nikalega.",
    "Aaj payment nahi hua toh connection cut ho jayega.",
])
def test_threats_worded_in_the_common_other_ways_still_count(threat):
    assert "payment_demand" in evidence_in(threat, ER, RULES.negation)


def test_none_of_the_five_scam_scripts_contains_a_line_where_a_warning_word_hid_evidence():
    for script in SCAM:
        for line in script["lines"]:
            hidden = set(evidence_in(line, ER, NO_NEGATION)) - set(evidence_in(line, ER, RULES.negation))
            _, _, ignored = score_text_ex(RULES, line)
            assert not hidden and not any(ignored.values()), (script["n"], line)


# ---------------------------------------------------------------- bits that changed along the way

def test_script_5_digit_readback_is_no_longer_replaced_by_the_gate():
    # a repeat of a number already held, spoken as digit words, used to be read back from a finding the record step had
    # discarded; the gate (correctly) refused it and Uncle said "samajh nahin aaya" in the middle of a dictation
    brain = new_brain()
    play(brain, SCRIPTS[4]["lines"])
    assert "replaced" not in actions(brain)


def test_the_exit_lines_pass_their_lint_and_a_rude_or_dishonest_one_does_not():
    groups = load_gate_lines()
    assert lint_gate_lines(groups) == [] and len(groups["exit_polite"]) >= 2
    bad = dict(groups)
    bad["exit_polite"] = [GateLine("exit_polite_01", "exit_polite", "Main ek automated assistant hoon. Ye call khatam hoti hai.", True),
                          GateLine("exit_polite_02", "exit_polite", "Shukriya ji, ye call ab khatam hoti hai.", True)]
    problems = lint_gate_lines(bad)
    assert any("polite" in p for p in problems) and any("automated assistant" in p for p in problems)


def test_the_score_parts_are_listed_for_the_log():
    brain = new_brain()
    play(brain, SCAM[3]["lines"][:2])
    parts = scam_score_parts(brain.state, CFG)
    assert parts["payment_demand"] == 2.0 and parts["threat"] == 1.0 and brain.state.evidence["payment_demand"]["turn"] == 2
