"""When Uncle may reply (the hold rule), and clean-up of repeated numbers and number fragments.

The replay tests use the two real good-microphone calls saved as fixtures (transcript text, arrival times and
utterance ids only: no audio, no key). Nothing here uses the network.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mirage.config import FSMConfig
from mirage.extract import Finding, Transcript, extract_intel, redundant_fragment, tidy_findings
from mirage.fsm import CallBrain
from mirage.replay import load_call, replay
from mirage.turns import TurnScheduler

CALLS = Path(__file__).parent / "fixtures" / "calls"
OFF = load_call(CALLS / "good_mic_keyterms_off.json")
ON = load_call(CALLS / "good_mic_keyterms_on.json")
PHONE = "0000274196"


def make(ceiling: float = 10.0, normal_wait: float = 0.0):
    brain = CallBrain(config=FSMConfig(synthetic_mode=True, normal_wait_s=normal_wait), seed=0)
    return brain, TurnScheduler(brain, ceiling)


# ------------------------------------------------------------------ the hold rule
def test_with_nothing_open_a_reply_is_due_as_soon_as_the_brains_wait_passes():
    brain, sched = make()
    assert sched.ready_at() is None                                        # nothing to answer yet
    sched.speech_start("u1", 0.5)
    sched.transcript("u1", "haan boliye", 1.0, 0.4)
    assert sched.ready_at() == 1.0 and sched.due(1.0) and sched.hold_reason(1.0) is None


def test_a_reply_is_held_while_a_new_utterance_is_open_then_covers_both():
    brain, sched = make()
    sched.transcript("u1", "mera naam Mohan hai", 1.0, 1.0)
    sched.speech_start("u2", 1.1)                                          # the caller carried on
    assert not sched.due(5.0) and "u2" in sched.hold_reason(5.0)
    sched.transcript("u2", "main CBI se bol raha hoon", 6.0, 3.0)
    assert sched.due(6.0)
    turn = sched.respond(6.0)
    assert "CBI" in brain.transcripts[1].text and brain.state.answered == 2 and turn.plan.text


def test_an_empty_transcript_clears_the_hold_and_adds_nothing():
    """6 of 19 segments came back empty in one real call: a noise blip must not freeze Uncle."""
    brain, sched = make()
    sched.transcript("u1", "haan ji boliye", 1.0, 0.8)
    sched.speech_start("u2", 1.2)
    assert not sched.due(1.5)
    sched.transcript("u2", "", 1.7, 0.5)                                   # a blip: speech_start, empty transcript
    assert sched.due(1.7) and len(brain.transcripts) == 1
    assert any(e["event"] == "empty_transcript_cleared_hold" for e in sched.log)
    assert sched.respond(1.7).plan.text


def test_a_stuck_utterance_stops_blocking_at_the_ceiling_and_a_late_transcript_is_a_new_turn():
    brain, sched = make(ceiling=10.0)
    sched.speech_start("u1", 0.2)
    sched.transcript("u1", "haan ji boliye", 1.0, 0.8)
    sched.speech_start("stuck", 1.1)                                       # its transcript never comes
    assert sched.ready_at() == pytest.approx(11.1)
    assert not sched.due(11.0) and sched.due(11.1)
    turn = sched.respond(11.1)
    assert turn.plan.text and brain.state.answered == 1
    assert [e["event"] for e in sched.log] == ["hold_ceiling"] and sched.log[0]["open_for_s"] == pytest.approx(10.0)
    assert "stuck" not in sched.open and "stuck" in sched.released
    sched.transcript("stuck", "arre suniye", 20.0, 2.0)                    # it finally arrives
    assert any(e["event"] == "late_transcript" for e in sched.log)
    assert sched.ready_at() == 20.0 and len(sched.pending()) == 1


def test_several_open_utterances_hold_until_the_last_one_closes_or_expires():
    brain, sched = make(ceiling=10.0)
    sched.transcript("u1", "haan ji", 1.0, 0.5)
    sched.speech_start("a", 2.0)
    sched.speech_start("b", 6.0)
    assert sched.ready_at() == pytest.approx(16.0)                         # the later of the two ceilings
    sched.transcript("a", "pehli baat", 7.0, 4.0)
    assert sched.ready_at() == pytest.approx(16.0)                         # b is still open
    sched.transcript("b", "doosri baat", 8.0, 1.0)
    assert sched.ready_at() == 8.0


def test_in_digit_mode_the_longer_wait_applies_and_a_start_inside_it_holds():
    brain, sched = make()
    sched.transcript("u1", "phone number hai zero zero", 1.0, 2.0)         # ends in digit words: 1.8 s wait
    assert sched.ready_at() == pytest.approx(2.8)
    sched.speech_start("u2", 1.3)
    assert sched.ready_at() == pytest.approx(11.3) and not sched.due(2.8)


def test_nothing_is_due_after_the_call_has_ended_and_a_transcript_without_a_start_is_fine():
    brain, sched = make()
    sched.transcript("u9", "hello", 1.0, 0.5)                              # no speech_start was ever seen
    assert any(e["event"] == "transcript_without_start" for e in sched.log) and sched.due(1.0)
    brain.state.ended = True
    assert sched.ready_at() is None


def test_the_hold_settings_come_from_the_config_and_are_marked_as_guesses():
    cfg = FSMConfig()
    assert (cfg.normal_wait_s, cfg.hold_ceiling_s, cfg.hold_total_cap_s, cfg.hold_total_cap_digit_s) == (0.3, 15.0, 20.0, 40.0)
    brain = CallBrain(config=FSMConfig(hold_ceiling_s=7.5), seed=0)
    assert TurnScheduler(brain).ceiling == 7.5
    source = (Path(__file__).parent.parent / "mirage" / "config.py").read_text(encoding="utf-8")
    for name in ("normal_wait_s", "hold_ceiling_s", "hold_total_cap_s", "hold_total_cap_digit_s"):
        assert "GUESS" in source.split(f"{name}: float")[1][:400]


def test_a_chain_of_utterances_is_answered_at_the_total_cap_and_the_next_one_is_a_new_turn():
    brain, sched = make(ceiling=15.0)
    sched.speech_start("u0", 0.2)
    sched.transcript("u0", "namaste ji", 1.0, 1.0)
    words = ["pehli", "doosri", "teesri"]
    for i, word in enumerate(words, start=1):             # each utterance starts right after the previous transcript
        sched.speech_start(f"u{i}", 1.0 + 6.0 * (i - 1) + 0.1)
        sched.transcript(f"u{i}", f"{word} baat suniye", 1.0 + 6.0 * i, 5.0)
    sched.speech_start("u4", 19.1)                         # still being spoken at 21 s
    assert sched.ready_at() == pytest.approx(21.0)         # 1.0 (oldest unanswered) + the 20 s cap, not u4's 15 s ceiling (34.1)
    turn = sched.respond(21.0)
    assert turn.plan.text and brain.state.answered == 4
    assert [e["event"] for e in sched.log] == ["hold_total_cap"] and sched.log[0]["unanswered_for_s"] == pytest.approx(20.0)
    sched.transcript("u4", "chauthi baat suniye", 25.0, 6.0)
    assert any(e["event"] == "late_transcript" for e in sched.log) and len(sched.pending()) == 1      # heard as the next turn


def test_while_the_caller_is_mid_number_the_longer_total_cap_applies():
    brain, sched = make(ceiling=15.0)
    sched.transcript("u1", "phone number hai zero", 1.0, 2.0)               # ends in a digit: digit mode
    sched.speech_start("u2", 1.2)
    assert sched.total_cap_s() == 40.0 and sched.ready_at() == pytest.approx(16.2)      # the ceiling (1.2 + 15), well before the 41 s cap
    sched.speech_start("u3", 30.0)                                          # (a second open utterance, much later)
    assert sched.ready_at() == pytest.approx(41.0)                          # capped at 1.0 + 40, not at 30 + 15


# ------------------------------------------------------------------ the two real calls
@pytest.mark.parametrize("call", [OFF, ON], ids=["keyterms_off", "keyterms_on"])
def test_the_phone_number_comes_out_whole_and_once_and_uncle_does_not_talk_over_the_dictation(call):
    result = replay(call)
    intel = result.brain.state.intel
    assert [f.value for f in intel["phone"]] == [PHONE] and [f.value for f in intel["case"]] == ["47291"]
    assert "ambiguous" not in intel                                         # no stray fragment beside the real number
    last_piece = max(r["t"] for r in call["transcripts"] if r["text"].strip())
    dictation_start = min(r["t"] for r in call["transcripts"] if "Zero" in r["text"] or "zero" in r["text"])
    assert all(not (dictation_start < t["at"] < last_piece) for t in result.turns)       # silent while the number is read
    per_turn = [f for t in result.turns for f in t["findings"] if f[0] == "phone"]
    assert len(per_turn) == 1                                               # the repeat is not a second finding


@pytest.mark.parametrize("call", [OFF, ON], ids=["keyterms_off", "keyterms_on"])
def test_the_number_comes_out_right_even_if_it_had_not_been_repeated_at_the_end(call):
    result = replay(call, drop_last=1)
    phones = result.brain.state.intel.get("phone", [])
    assert [f.value for f in phones] == [PHONE] and phones[0].confidence == "keyword"
    assert "ambiguous" not in result.brain.state.intel
    assert "जीरो" in phones[0].quote and " | " in phones[0].quote           # built from the joined pieces


def test_without_the_hold_the_old_failure_comes_back():
    """The test can fail: with the hold switched off (ceiling 0), Uncle answers at 19.4 s, mid-number, and the number is lost."""
    result = replay(OFF, ceiling_s=0.0, drop_last=1)
    assert "phone" not in result.brain.state.intel
    assert [f.value for f in result.brain.state.intel["ambiguous"]] == ["00274196"]
    assert any(abs(t["at"] - 19.39) < 0.1 for t in result.turns)


def test_with_the_chosen_defaults_the_whole_monologue_and_dictation_is_answered_once_and_correctly():
    for call, at in ((OFF, 33.6), (ON, 34.13)):
        result = replay(call)                                               # config defaults: 0.3 s wait, 15 s ceiling, 20 s / 40 s caps
        assert len(result.turns) == 1 and result.turns[0]["at"] == pytest.approx(at, abs=0.01)
        assert [f.value for f in result.brain.state.intel["phone"]] == [PHONE] and result.scheduler.log == []


def test_a_flat_20_s_cap_would_have_answered_mid_number_which_is_why_digit_mode_gets_a_longer_one():
    result = replay(OFF, total_cap_s=20.0)                                  # override the digit-mode extension
    assert result.turns[0]["at"] == pytest.approx(23.05, abs=0.01)         # answered during the dictation
    assert any(e["event"] == "hold_total_cap" for e in result.scheduler.log)


def test_the_ceiling_can_fire_on_genuine_long_speech_which_is_the_known_trade_off():
    """A 13.5 s utterance is real speech in the recorded call. With a 0.3 s normal wait the hold sees it, and a 10 s ceiling
    releases it mid-speech; a 15 s ceiling does not."""
    short = replay(OFF, normal_wait_s=0.3, ceiling_s=10.0)
    assert any(e["event"] == "hold_ceiling" for e in short.scheduler.log) and short.turns[0]["at"] == pytest.approx(13.05, abs=0.01)
    long = replay(OFF, normal_wait_s=0.3, ceiling_s=15.0)
    assert not any(e["event"] == "hold_ceiling" for e in long.scheduler.log)


def test_a_stuck_utterance_and_a_noise_blip_inside_a_real_call_do_not_stall_it():
    call = {"speech_starts": [{"utterance_id": "n1", "t": 5.0}, {"utterance_id": "u1", "t": 0.2},
                              {"utterance_id": "stuck", "t": 20.0}, {"utterance_id": "u3", "t": 21.0},
                              {"utterance_id": "u2", "t": 30.5}],
            "transcripts": [{"utterance_id": "u1", "t": 1.0, "text": "namaste main CBI se bol raha hoon", "audio_s": 2.0},
                            {"utterance_id": "n1", "t": 5.5, "text": "", "audio_s": 0.5},                    # a blip
                            {"utterance_id": "u3", "t": 22.0, "text": "suniye suniye", "audio_s": 1.0},       # waits for the stuck one
                            {"utterance_id": "u2", "t": 31.0, "text": "aapka account block ho jayega", "audio_s": 1.5}]}
    result = replay(call, ceiling_s=10.0, normal_wait_s=0.0)
    assert [round(t["at"], 1) for t in result.turns] == [1.0, 30.0, 31.0]
    assert [e["event"] for e in result.scheduler.log if e["event"] != "empty_transcript_cleared_hold"] == ["hold_ceiling"]
    assert any(e["event"] == "empty_transcript_cleared_hold" for e in result.scheduler.log)


# ------------------------------------------------------------------ repeated numbers and fragments
def finding(kind: str, value: str, i: int = 0) -> Finding:
    return Finding(kind=kind, value=value, valid=True, reason="", confidence="none", needs_confirmation=True,
                   quote=value, t=float(i), source_index=i)


def test_the_same_number_said_twice_in_one_turn_is_one_finding():
    texts = [Transcript("phone number hai zero zero zero zero do saat chaar ek nau chhe", 5.0, 5.0),
             Transcript("haan ji meri baat suniye aap pareshan mat hoiye", 12.0, 3.0),
             Transcript("phir se bolta hoon phone number zero zero zero zero do saat chaar ek nau chhe", 40.0, 6.0)]
    found = extract_intel(texts, synthetic_mode=True)
    assert [(f.kind, f.value) for f in found] == [("phone", PHONE)]
    assert found[0].source_index == 0                                       # kept at its first mention


def test_a_fragment_is_dropped_once_a_number_in_the_same_list_contains_it():
    found = tidy_findings([finding("ambiguous", "00274196"), finding("phone", PHONE, 1)])
    assert [(f.kind, f.value) for f in found] == [("phone", PHONE)]
    found = tidy_findings([finding("ambiguous", PHONE), finding("phone", PHONE, 1)])      # the same digits, labelled
    assert [f.kind for f in found] == ["phone"]
    found = tidy_findings([finding("ambiguous", "2741"), finding("ambiguous", "00274196", 1)])
    assert [f.value for f in found] == ["00274196"]                         # a shorter unlabelled piece of a longer one


def test_a_different_number_is_never_merged_and_an_unrelated_fragment_stays():
    found = tidy_findings([finding("phone", PHONE), finding("phone", "0000555555", 1), finding("ambiguous", "9876", 2)])
    assert [f.value for f in found] == [PHONE, "0000555555", "9876"]
    assert tidy_findings([finding("phone", PHONE), finding("account", PHONE, 1)]) and len(
        tidy_findings([finding("phone", PHONE), finding("account", PHONE, 1)])) == 2          # different kinds are not duplicates
    assert not redundant_fragment("0000274196", [("ambiguous", "0000274196")])               # never dropped by an equal ambiguous


def test_the_stored_intel_drops_an_earlier_fragment_when_a_labelled_number_arrives_and_ignores_a_later_one():
    brain = CallBrain(config=FSMConfig(synthetic_mode=True), seed=0)
    brain._record_intel((finding("ambiguous", "00274196"),))
    assert [f.value for f in brain.state.intel["ambiguous"]] == ["00274196"]
    brain._record_intel((finding("phone", PHONE, 1),))
    assert "ambiguous" not in brain.state.intel and [f.value for f in brain.state.intel["phone"]] == [PHONE]
    brain._record_intel((finding("ambiguous", "274196", 2),))                # a piece of what we already hold
    assert "ambiguous" not in brain.state.intel
    brain._record_intel((finding("ambiguous", "55512", 3), finding("phone", "0000555559", 4)))
    assert [f.value for f in brain.state.intel["ambiguous"]] == ["55512"]    # unrelated fragment stays
    assert [f.value for f in brain.state.intel["phone"]] == [PHONE, "0000555559"]


def test_the_scripted_calls_in_the_other_suites_are_unchanged_by_the_tidying():
    """A single clean number still produces exactly its one finding."""
    found = extract_intel([Transcript("mera number hai zero zero zero zero do saat chaar ek nau chhe", 2.0, 4.0)], synthetic_mode=True)
    assert [(f.kind, f.value) for f in found] == [("phone", PHONE)]
