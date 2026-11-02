"""Tests for the call brain (mirage/fsm.py) and its runner. Nothing here touches the network."""
from __future__ import annotations

import dataclasses
import subprocess
import sys
from dataclasses import replace

import pytest

import mirage.fsm as fsm_module
from mirage.config import ROOT, FSMConfig, LLMConfig, ModelBudget, default_config
from mirage.extract import Transcript, ends_with_digits, extract_intel
from mirage.fsm import CallBrain, ReplyPlan
from mirage.llm import LLMResult, LLMRouter, UsageStore
from mirage.persona import load_persona
from mirage.spoken import render_fact, speak_code, speak_digits

OBSTACLES = ("app_not_opening", "otp_late", "limit_exceeded_second_account")
# A question no template and no signal covers (INVENTED): this is when the optional LLM may word a stall line.
PERSONAL_QUESTION = "aap pehle kahan kaam karte the, kitne saal naukri ki?"

# A whole call, from the hook to the hang-up. The caller lines are INVENTED.
SCRIPT = [
    "hello",
    "main CBI officer bol raha hoon, aapke naam se parcel pakda gaya hai",
    "aapke khilaf arrest warrant nikla hai, drugs mile hain",
    "case number hai chaar saat do nau ek",
    "aap kahan gaye, kitna time lagega, jaldi dhoondhiye",
    "ab payment karna hoga, UPI ID bataiye",
    "UPI ID hai rajesh dot test at ybl",
    "account number hai ek do teen chaar paanch chhe saat aath nau zero",
    "IFSC code hai M I R G zero zero zero zero ek do teen",
    "jaldi karo abhi anydesk install karo",
    "OTP bataiye turant",
    "payment kyun nahi ho raha, jaldi karo warna arrest",
    "apna phone number bataiye",
    "time waste mat karo, main phone rakhta hoon, bye",
]


def run(brain: CallBrain, lines: list[str], start_t: float = 0.0):
    """Feed lines as separate turns, 6 s apart."""
    turns, t = [], start_t
    for line in lines:
        t += 6.0
        brain.hear(line, t, audio_s=2.0)
        turns.append(brain.respond())
    return turns


def say(brain: CallBrain, line: str, gap: float = 6.0):
    last = brain.transcripts[-1].t if brain.transcripts else 0.0
    brain.hear(line, last + gap, audio_s=2.0)
    return brain.respond()


def assert_traceable(plan: ReplyPlan, brain: CallBrain) -> None:
    """Every digit Uncle says must be recorded with its origin, and nothing else may carry a number."""
    if plan.needs_gate:
        assert plan.text == "" and not plan.digit_sources
        return
    assert plan.text.strip()
    remaining = plan.text
    for s in plan.digit_sources:
        assert s.spoken in plan.text
        if s.origin == "persona":
            assert s.field in brain.persona.facts and s.digits in brain.persona.fact(s.field).value
        else:
            matches = [f for fs in brain.state.intel.values() for f in fs if f.quote == s.quote and s.digits in f.value]
            assert matches, f"digits {s.digits!r} do not match any caller finding"
            assert s.quote and s.source_index is not None and s.t is not None
        remaining = remaining.replace(s.spoken, "slot")
    assert not any(ch.isdigit() for ch in remaining)
    assert extract_intel([Transcript(remaining, 0.0)]) == []


# ---------------------------------------------------------------- full calls

def test_a_full_call_runs_from_the_hook_to_the_goodbye_on_templates_alone():
    brain = CallBrain(seed=3)
    assert brain.llm_enabled is False
    turns = run(brain, SCRIPT)
    assert [t.stage.stage for t in turns][-1] == "S5" and brain.state.ended
    assert {"S0", "S1", "S3", "S4", "S5"} <= {t.stage.stage for t in turns}
    stages = [t.stage.stage for t in turns]
    assert stages == sorted(stages), stages                      # this call only moves forward
    for turn in turns:
        assert turn.plan.source == "template" and not turn.plan.needs_gate
        assert_traceable(turn.plan, brain)
    tactics = [t.plan.tactic for t in turns]
    assert all(tactics.count(o) <= 1 for o in OBSTACLES)         # one-shot per call
    assert {"app_not_opening", "otp_late", "give_own_detail", "readback_partial", "goodbye_wrap"} <= set(tactics)


def test_the_same_call_finishes_when_every_model_is_out_of_budget(tmp_path):
    class Exploding:
        def generate(self, *args):
            raise AssertionError("a model was called although the budget is zero")

    cfg = dataclasses.replace(default_config(), models=(ModelBudget("A", "A", 10, 0), ModelBudget("B", "B", 10, 0)),
                              usage_path=tmp_path / "u.json", first_429_path=tmp_path / "f.txt")
    store = UsageStore(cfg.usage_path, reset_timezone=cfg.reset_timezone, reset_hour=cfg.reset_hour)
    router = LLMRouter(cfg, Exploding(), store)
    calls = []
    original = router.complete
    router.complete = lambda *a: (calls.append(a), original(*a))[1]

    brain = CallBrain(seed=3, router=router, llm_enabled=True)
    lines = SCRIPT[:3] + [PERSONAL_QUESTION] + SCRIPT[3:]
    turns = run(brain, lines)
    assert calls, "the LLM path was never even tried, so this test proved nothing"
    assert turns[-1].stage.stage == "S5"
    for turn in turns:
        assert turn.plan.source == "template"
        assert_traceable(turn.plan, brain)


def test_the_llm_is_off_by_default_and_never_called_unless_enabled():
    class Spy:
        calls = 0

        def complete(self, *a):
            Spy.calls += 1
            return LLMResult("Achha achha, zara dheere boliye.", "x")

    brain = CallBrain(router=Spy())
    run(brain, ["hello", PERSONAL_QUESTION, PERSONAL_QUESTION])
    assert Spy.calls == 0


def test_an_accepted_llm_line_is_marked_and_a_bad_one_is_thrown_away():
    class Router:
        def __init__(self, text):
            self.text = text
            self.calls = 0

        def complete(self, *a):
            self.calls += 1
            return LLMResult(self.text, "x")

    good = Router("Achha achha, ye arrest kya hota hai, zara samjhaiye.")
    brain = CallBrain(router=good, llm_enabled=True)
    turns = run(brain, ["hello", PERSONAL_QUESTION])
    assert turns[1].plan.source == "llm" and turns[1].plan.text == good.text

    bad = Router("Mera account number 12345 hai beta.")
    brain = CallBrain(router=bad, llm_enabled=True)
    turns = run(brain, ["hello", PERSONAL_QUESTION])
    assert bad.calls == 1 and turns[1].plan.source == "template"


def test_the_llm_is_used_at_most_once_in_a_few_turns():
    class Router:
        calls = 0

        def complete(self, *a):
            Router.calls += 1
            return LLMResult("Achha achha, zara dheere samjhaiye na.", "x")

    brain = CallBrain(router=Router(), llm_enabled=True)
    run(brain, ["hello"] + [PERSONAL_QUESTION] * 4)
    assert Router.calls == 2          # turns 1 and 4, with min gap 3


# ---------------------------------------------------------------- triggers and one-shot obstacles

def at_stage(stage: str, **kw) -> CallBrain:
    brain = CallBrain(**kw)
    brain.state.stage, brain.state.turn = stage, 1
    return brain


@pytest.mark.parametrize("ask, obstacle", [("anydesk install karo abhi", "app_not_opening"), ("OTP bataiye abhi", "otp_late")])
def test_an_obstacle_fires_only_after_its_trigger_and_only_once(ask, obstacle):
    brain = at_stage("S3")
    quiet = run(brain, ["ab payment karna hoga, paise bhejiye", "jaldi karo warna problem hogi"])
    assert obstacle not in [t.plan.tactic for t in quiet]        # nothing was asked: not used
    first = say(brain, ask)
    assert first.plan.tactic == obstacle
    again = say(brain, ask)
    assert again.plan.tactic != obstacle                         # one-shot per call
    assert obstacle in brain.state.used_obstacles


def test_the_limit_obstacle_needs_payment_details_to_have_been_given():
    brain = at_stage("S4")
    turns = run(brain, ["jaldi karo turant", "der mat karo warna problem hogi", "last chance hai"])
    assert "limit_exceeded_second_account" not in [t.plan.tactic for t in turns]

    brain = at_stage("S3")
    brain.state.turn = 1
    run(brain, ["UPI ID hai rajesh at ybl"])
    assert "asked_for_payment" in brain.state.flags
    later = run(brain, ["account number aur IFSC bhi likhiye", "IFSC code hai M I R G zero zero zero zero ek do teen",
                        "jaldi karo turant warna problem hogi"])
    tactics = [t.plan.tactic for t in later]
    assert tactics.count("limit_exceeded_second_account") <= 1


def test_the_obstacle_is_not_used_in_the_opening():
    brain = at_stage("S0")
    turn = say(brain, "anydesk install karo, OTP bataiye")
    assert turn.stage.stage == "S0" and turn.plan.tactic not in OBSTACLES
    assert {"asked_to_install_app", "asked_for_otp"} <= brain.state.flags      # remembered for later


def test_uncle_gives_his_own_detail_only_when_asked():
    brain = CallBrain(seed=2)
    turns = run(brain, SCRIPT[:12])
    assert "give_own_detail" not in [t.plan.tactic for t in turns]
    asked = say(brain, "apna phone number bataiye")
    assert asked.plan.tactic == "give_own_detail"
    assert [s.field for s in asked.plan.digit_sources] == ["phone"]
    assert asked.plan.digit_sources[0].digits == brain.persona.fact("phone").value


@pytest.mark.parametrize("ask, key", [("aapka naam bataiye", "name"), ("aapka bank bataiye", "bank"),
                                      ("aapka pata bataiye", "address"), ("aapka phone number bataiye", "phone"),
                                      ("aapka account number bataiye", "account"), ("aapka IFSC bataiye", "ifsc")])
def test_each_persona_fact_is_spoken_from_the_sheet_with_its_origin(ask, key):
    brain = at_stage("S3")
    turn = say(brain, ask)
    assert turn.plan.tactic == "give_own_detail"
    assert_traceable(turn.plan, brain)
    fact = load_persona().fact(key)
    assert fact.label in turn.plan.text
    if key in ("phone", "account", "ifsc", "address"):
        assert turn.plan.digit_sources and all(s.origin == "persona" and s.field == key for s in turn.plan.digit_sources)


def test_a_statement_about_the_callers_own_account_is_not_a_request_for_uncles():
    brain = at_stage("S1")
    assert say(brain, "aapka account band ho jayega").plan.tactic != "give_own_detail"


def test_a_credential_uncle_does_not_have_gets_a_stall():
    brain = at_stage("S3")
    turn = say(brain, "aapka aadhaar number bataiye")
    assert turn.plan.tactic == "stall_look_for_things" and not turn.plan.digit_sources


# ---------------------------------------------------------------- stages

def test_nothing_matching_keeps_the_stage_and_stalls():
    brain = CallBrain()
    run(brain, ["hello"])
    for line in ["haan ji", "okay theek hai", "mera naam Amit hai", "hmm"]:
        turn = say(brain, line)
        assert turn.stage.stage == "S0" and turn.plan.tactic == "generic_stall"


def test_s4_cannot_be_entered_from_the_early_stages():
    brain = at_stage("S1")
    turn = say(brain, "jaldi karo turant der mat karo warna problem")
    assert turn.stage.stage == "S1"                                # pressure words in S1 are still fear, not S4


def test_payment_evidence_moves_the_stage_to_s3_even_from_the_opening():
    brain = CallBrain()
    run(brain, ["hello"])
    turn = say(brain, "UPI ID hai rajesh at ybl")
    assert turn.stage.stage == "S3"


def test_goodbye_words_move_to_s5_from_anywhere():
    brain = at_stage("S2")
    assert say(brain, "theek hai baad mein baat karte hain, bye").stage.stage == "S5"


def test_moving_back_needs_a_large_margin_and_only_one_step():
    brain = at_stage("S4")
    assert say(brain, "arrest").stage.stage == "S4"                # one weak S1 word does not pull it back
    brain = at_stage("S4")
    assert say(brain, "ab payment karna hoga, UPI ID bataiye, account number likhiye").stage.stage == "S3"
    brain = at_stage("S3")
    assert say(brain, "aapke khilaf arrest warrant hai, CBI case darj hai").stage.stage == "S3"   # two steps back: no


def test_the_scenario_is_detected_once_and_can_be_forced():
    brain = CallBrain()
    run(brain, ["hello", "aapka KYC update karna hai, nahi toh account band ho jayega"])
    assert brain.state.scenario == "bank_kyc"
    run(brain, ["aapke naam se parcel aaya hai, drugs mile hain"])
    assert brain.state.scenario == "bank_kyc"                      # not flipped
    assert CallBrain(scenario="digital_arrest").state.scenario == "digital_arrest"


def test_alarm_lines_match_the_scenario():
    brain = CallBrain(scenario="bank_kyc", seed=5)
    run(brain, ["hello"])
    turn = say(brain, "aapke khilaf arrest warrant nikla hai")
    assert turn.plan.tactic == "alarm"
    line = next(ln for ln in brain.bank.lines if ln.id == turn.plan.line_id)
    assert line.scenario == "bank_kyc"


# ---------------------------------------------------------------- rotation

def test_a_stall_line_is_not_repeated_while_it_is_recent_and_runs_are_reproducible():
    def lines(seed):
        brain = CallBrain(seed=seed)
        run(brain, ["hello"])
        return [say(brain, "hmm").plan.line_id for _ in range(9)]

    first = lines(7)
    assert len(set(first[:4])) == 4                                # all four variants before any repeat
    assert all(a != b for a, b in zip(first, first[1:]))           # never the same line twice in a row
    assert lines(7) == first
    assert lines(8) != first or True                               # another seed may differ; no promise either way


# ---------------------------------------------------------------- digit mode, joining, expected hint

def test_digit_mode_waits_longer_and_the_wait_is_configurable():
    brain = CallBrain()
    run(brain, ["hello"])
    assert brain.reply_wait_s() == 0.0
    brain.hear("mera number hai chaar saat do nau ek", 20.0, 2.0)          # ends in the middle of a number
    assert brain.reply_wait_s() == 1.8
    brain.respond()
    after_readback = brain.state.awaiting_digits
    assert after_readback and brain.reply_wait_s() == 1.8                  # Uncle asked for the rest: still waiting
    assert CallBrain(config=FSMConfig(digit_wait_s=2.2)).cfg.digit_wait_s == 2.2
    slow = CallBrain(config=FSMConfig(digit_wait_s=2.2))
    slow.hear("ek do teen", 5.0, 1.0)
    assert slow.reply_wait_s() == 2.2


def test_transcripts_that_arrive_close_together_are_joined_and_far_ones_are_not():
    brain = at_stage("S1")
    brain.hear("account number hai chaar zero zero teen", 10.0, 2.0)
    brain.hear("nau ek aath do saat", 11.0, 1.5)
    joined = brain.respond().findings
    assert [f.value for f in joined] == ["400391827"]

    brain = at_stage("S1")
    brain.hear("account number hai chaar zero zero teen", 10.0, 2.0)
    brain.hear("nau ek aath do saat", 40.0, 1.5)
    apart = brain.respond().findings
    assert "400391827" not in [f.value for f in apart]


def test_the_expected_hint_is_passed_to_the_extractor_after_a_specific_question(monkeypatch):
    seen = []
    original = fsm_module.extract_intel

    def spy(transcripts, **kwargs):
        seen.append(kwargs.get("expected"))
        return original(transcripts, **kwargs)

    monkeypatch.setattr(fsm_module, "extract_intel", spy)
    brain = at_stage("S1")
    asked = None
    for _ in range(5):                                                      # keep the caller in S1 until Uncle asks for the case number
        turn = say(brain, "aapke khilaf arrest warrant hai, CBI case darj hai")
        if turn.plan.expected == "case":
            asked = turn
            break
    assert asked and asked.plan.tactic == "ask_detail_slow"
    assert seen[0] is None
    say(brain, "chaar saat do nau ek")
    assert seen[-1] == "case"                                               # passed because Uncle just asked for it
    say(brain, "haan ji")                                                   # Uncle read part back and asked for the rest: hint again
    assert seen[-1] == "case"
    say(brain, "haan ji")                                                   # that reply was a plain stall: no hint now
    assert seen[-1] is None


# ---------------------------------------------------------------- read-back

def test_a_readback_uses_only_the_callers_digits_and_records_where_they_came_from():
    brain = at_stage("S3")
    turn = say(brain, "account number hai chaar zero zero teen nau ek aath do saat")
    plan = turn.plan
    assert plan.tactic == "readback_partial"
    finding = turn.findings[0]
    assert len(plan.digit_sources) == 1
    source = plan.digit_sources[0]
    assert source.origin == "caller" and source.quote == finding.quote and source.source_index == finding.source_index
    assert finding.value.startswith(source.digits) and len(source.digits) < len(finding.value)   # a part, never the whole
    assert 3 <= len(source.digits) <= 6
    assert source.spoken == "4 0 0, 3 9"          # spaced numerals, the chosen default
    assert_traceable(plan, brain)
    assert plan.awaiting_digits and plan.expected == "account"


def test_no_readback_without_a_finding():
    brain = at_stage("S3")
    turn = say(brain, "mera number hai, aap likh lijiye")
    assert turn.plan.tactic != "readback_partial" and not turn.plan.digit_sources


def test_the_same_number_is_not_read_back_twice_and_readbacks_are_capped():
    brain = at_stage("S3")
    first = say(brain, "case number hai chaar saat do nau ek")
    assert first.plan.tactic == "readback_partial"
    second = say(brain, "case number hai chaar saat do nau ek")
    assert second.plan.tactic != "readback_partial"
    for n in range(5):
        say(brain, f"account number hai {'ek do teen chaar paanch chhe saat aath nau'.split()[n]} do teen chaar paanch chhe")
    assert brain.state.readbacks <= brain.cfg.max_readbacks
    assert all(v <= 2 for v in brain.state.readbacks_by_kind.values())


def test_an_ifsc_readback_spells_letters_and_never_invents_digits():
    brain = at_stage("S3")
    turn = say(brain, "IFSC code hai M I R G zero zero zero zero ek do teen")
    assert turn.plan.tactic == "readback_partial"
    assert_traceable(turn.plan, brain)
    assert "M I R" in turn.plan.text


# ---------------------------------------------------------------- needs_gate and the gate hook

@pytest.mark.parametrize("line, reason", [
    ("are you a robot?", "ai_question"), ("kya aap insaan hain ya machine", "ai_question"),
    ("aap AI hain kya", "ai_question"), ("ambulance bulao, emergency hai", "emergency"),
    ("mere bete ka accident ho gaya, hospital jaana hai", "emergency"),
])
def test_a_sincere_identity_question_or_an_emergency_gets_no_line(line, reason):
    brain = at_stage("S3")
    before = set(brain.state.used_obstacles)
    turn = say(brain, line)
    plan = turn.plan
    assert plan.needs_gate and plan.gate_reason == reason
    assert plan.text == "" and plan.source == "needs_gate" and plan.line_id is None and not plan.digit_sources
    assert brain.state.used_obstacles == before and not brain.state.recent


def test_the_gate_hook_sees_every_plan_and_its_replacement_is_used():
    seen = []

    def gate(plan, state, ctx):
        seen.append((plan, ctx))
        return replace(plan, text="GATED") if plan.text else plan

    brain = CallBrain(gate=gate)
    turns = run(brain, ["hello", "are you a robot?", "arrest warrant hai"])
    assert len(seen) == 3 and seen[1][0].needs_gate
    assert [c.turn for _, c in seen] == [0, 1, 2] and seen[1][1].text == "are you a robot?"
    assert turns[0].plan.text == "GATED" and turns[1].plan.text == ""


# ---------------------------------------------------------------- the pieces

def test_digits_are_spoken_in_small_groups_and_facts_render_from_the_sheet():
    assert speak_digits("0047138") == "0 0 4, 7 1 3, 8"                         # the default is spaced numerals
    assert speak_digits("0047138", style="words") == "zero zero chaar, saat ek teen, aath"
    assert speak_code("MIRG00") == "M I R, G 0 0"
    persona = load_persona()
    spoken, pairs = render_fact(persona.fact("address"))
    assert "Flat 1 2," in spoken and pairs == [("12", "1 2")]
    with pytest.raises(ValueError):
        speak_digits("12a")
    with pytest.raises(ValueError):
        speak_digits("12", style="numerals")                                    # the old name is gone: one name per style


def test_spaced_numerals_are_the_default_in_the_config_and_in_every_spoken_digit_string():
    import re
    from mirage.spoken import DEFAULT_DIGIT_STYLE
    assert DEFAULT_DIGIT_STYLE == FSMConfig().digit_style == "spaced"
    persona = load_persona()
    # Uncle's own details, straight from the fact sheet
    for key in ("phone", "account"):
        spoken, _ = render_fact(persona.fact(key))
        assert re.fullmatch(r"[0-9 ,]+", spoken), spoken
    assert re.fullmatch(r"[A-Z0-9 ,]+", render_fact(persona.fact("ifsc"))[0])
    # ... through the brain, for each fact the caller can ask about
    for ask, key in [("aapka phone number bataiye", "phone"), ("aapka account number bataiye", "account"),
                     ("aapka IFSC bataiye", "ifsc"), ("aapka pata bataiye", "address")]:
        brain = at_stage("S3")
        plan = say(brain, ask).plan
        assert plan.tactic == "give_own_detail" and plan.digit_sources
        for source in plan.digit_sources:
            assert not re.search(r"zero|chaar|paanch|saat|aath", source.spoken), source.spoken
            assert re.fullmatch(r"[A-Z0-9 ,]+", source.spoken) or key == "address"
        assert not any(w in plan.text for w in ("zero", "chaar", "paanch", "chhe ", "saat ", "aath"))
    # ... and in a read-back of the caller's own digits
    brain = at_stage("S3")
    plan = say(brain, "account number hai chaar zero zero teen nau ek aath do saat").plan
    assert plan.tactic == "readback_partial" and re.fullmatch(r"[0-9 ,]+", plan.digit_sources[0].spoken)
    # the style can still be switched in the config, and then it is used everywhere
    brain = CallBrain(config=FSMConfig(digit_style="words"))
    brain.state.stage, brain.state.turn = "S3", 1
    assert say(brain, "aapka phone number bataiye").plan.digit_sources[0].spoken.startswith("zero zero zero, zero do")


def test_ends_with_digits():
    assert ends_with_digits("mera number hai chaar saat")
    assert ends_with_digits("chaar, saat,")
    assert not ends_with_digits("chaar saat hai na")
    assert not ends_with_digits("")


# ---------------------------------------------------------------- the terminal runner

def run_runner(stdin: str, *args: str) -> str:
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_call.py"), *args], input=stdin,
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_runner_shows_stage_tactic_line_and_findings_without_the_network():
    out = run_runner("hello\nCBI officer, aapke khilaf arrest warrant hai\ncase number hai chaar saat do nau ek\n:findings\n:state\n")
    assert "LLM off" in out and "stage:" in out and "tactic:" in out and "Uncle:" in out
    assert "source:   template" in out and "findings this turn:" in out and "quote=" in out
    assert "digits '472' came from" in out or "digits '4729' came from" in out
    assert "wait before the next reply" in out


def test_the_runner_prints_needs_gate_clearly_when_the_gate_is_off():
    out = run_runner("hello\nare you a robot?\n", "--no-gate")
    assert "NEEDS GATE: ai_question" in out and "gate is off" in out and "GATE OFF" in out


def test_the_runner_uses_the_gate_by_default_and_shows_what_it_did():
    out = run_runner("hello\nare you a robot?\n")
    assert "gate on" in out and "GATE:     disclose" in out and "automated assistant" in out
    assert "THE CALL ENDS HERE" in out and "NEEDS GATE" not in out


def test_the_runner_joins_a_dictation_given_with_plus():
    out = run_runner("hello\n+ account number hai chaar zero zero teen\nnau ek aath do saat\n")
    assert "400391827" in out and "digit mode" in out


def test_the_runner_stays_off_the_llm_unless_asked():
    out = run_runner("hello\n")
    assert "LLM off" in out and "WARNING" not in out


# ---------------------------------------------------------------- found in the owner's runner session

def test_weak_evidence_for_the_stage_the_call_is_already_in_gets_that_stages_tactic_not_a_stall():
    brain = CallBrain(seed=1)
    run(brain, ["hello"])
    turn = say(brain, "Kya aapne Mumbai se koi parcel order kiya tha?")          # one hook word (1.5, below the minimum of 2)
    assert turn.stage.stage == "S0" and turn.plan.tactic == "mishear_name"


def test_weak_evidence_for_a_different_stage_does_not_move_it_and_still_stalls():
    brain = at_stage("S1")
    turn = say(brain, "Thoda wait karna padega")    # one S2 word at 1.0: below the minimum
    assert turn.stage.stage == "S1" and turn.plan.tactic == "generic_stall"


@pytest.mark.parametrize("ask, key", [("aap kahan rehte hain?", "address"), ("apna account number confirm kijiye", "account"),
                                      ("aap kis bank mein hain", "bank"), ("aapka naam kya hai", "name")])
def test_more_ways_of_asking_for_uncles_own_detail(ask, key):
    brain = at_stage("S3")
    turn = say(brain, ask)
    assert turn.plan.tactic == "give_own_detail", turn.plan
    assert load_persona().fact(key).label in turn.plan.text
    assert_traceable(turn.plan, brain)



# ---------------------------------------------------------------- needs_gate matches whole words only

ORDINARY_LINES = [   # INVENTED ordinary lines: none of them may ask the gate for anything
    "Mai bank se bol raha hoon, aapka account band hai", "Kai baar bataya hai aapko", "Bhai sahab, kaise hain aap",
    "Chai peeni hai kya", "Maine kaha tha ki paisa jaldi bhejiye", "Main aapki madad ke liye hoon", "Mail aaya hai kya",
    "Aaj ka din kaisa hai", "Rain ho rahi hai, isliye der hui", "Aap wait kijiye, hai na", "Simple si baat hai",
    "है", "यह मेरा घर है", "कई बार बताया है", "मैं बैंक से बोल रहा हूँ", "आपका खाता बंद है", "ऐसा मत कीजिए",
    "भाई साहब, कैसे हैं आप", "बीमा कराया है", "मशीनी तरीके से नहीं, ध्यान से सुनिए", "डेथ सर्टिफिकेट नहीं चाहिए",
]
GATE_LINES = [
    ("are you a robot?", "ai_question"), ("Are you AI?", "ai_question"), ("are you A.I.?", "ai_question"),
    ("kya aap insaan hain", "ai_question"), ("क्या आप रोबोट हैं", "ai_question"), ("क्या आप एआई हैं", "ai_question"),
    ("क्या आप ए आई हैं", "ai_question"), ("is this a machine talking", "ai_question"),
    ("ambulance bulao", "emergency"), ("इमरजेंसी है", "emergency"), ("accident ho gaya hai", "emergency"),
]


@pytest.mark.parametrize("line", ORDINARY_LINES)
def test_ordinary_hinglish_never_asks_for_the_gate(line):
    from mirage.fsm import detect_signals, load_rules
    assert not {"ai_question", "emergency"} & set(detect_signals(load_rules(), line).names)
    brain = at_stage("S1")
    assert not say(brain, line).plan.needs_gate


@pytest.mark.parametrize("line, reason", GATE_LINES)
def test_real_identity_questions_and_emergencies_still_ask_for_the_gate(line, reason):
    brain = at_stage("S1")
    turn = say(brain, line)
    assert turn.plan.needs_gate and turn.plan.gate_reason == reason
