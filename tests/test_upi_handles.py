"""Tests for joining a bare "OK" with a provider word, and for ranking the handle guesses. Taken from the owner's demo call of 2026-11-03
(id 20261103_174137_097a) and the earlier rehearsal call (20261103_162757_6dd0): STAGED development data, not an evaluation. The texts
are the transcripts as logged in data/calls/. Nothing touches the network."""
from __future__ import annotations

import pytest

from mirage.config import FSMConfig, KNOWN_UPI_PROVIDERS
from mirage.extract import OK_WORDS, Transcript, extract_intel
from mirage.fsm import CallBrain

# (arrival time, seconds of audio, text) as logged for the demo call; the groups are the turns the brain answered together.
DEMO_TURNS = [
    [(73.266, 4.628, "Verification के लिए refundable security deposit भेजना होगा।")],
    [(84.188, 4.372, "UPI ID है verify officer one"), (87.782, 3.68, "Two three at OK axis")],
]


def replay(turns) -> CallBrain:
    brain = CallBrain(config=FSMConfig(synthetic_mode=True), seed=1)
    for group in turns:
        for t, audio_s, text in group:
            brain.hear(text, t, audio_s=audio_s)
        brain.respond()
    return brain


def vpas(*texts: str):
    return [f for f in extract_intel([Transcript(x, 3.0 * i, 2.0) for i, x in enumerate(texts)], synthetic_mode=True) if f.kind == "vpa"]


# ---------------------------------------------------------------- 1. "at OK axis" gives @okaxis, still unconfirmed

def test_the_demo_call_ok_axis_is_joined_into_okaxis():
    (f,) = replay(DEMO_TURNS).state.intel["vpa"]
    assert f.value == "verifyofficer123@okaxis"
    assert f.confidence == "heard" and f.needs_confirmation and f.valid         # joined words are not a confirmed handle
    assert "joined" in f.reason and "OK axis" in f.reason


def test_the_quote_keeps_the_words_exactly_as_heard():
    (f,) = replay(DEMO_TURNS).state.intel["vpa"]
    assert f.quote == "verify officer one | Two three at OK axis"
    assert f.t == 84.188 and f.suggested is None and f.candidates == ()          # nothing to guess: the handle is read, not guessed


def test_the_stored_demo_value_before_the_join_was_the_bare_ok():
    """What the live call stored (findings.json of the demo call) was verifyofficer123@OK: kept here so the change is visible."""
    (f,) = vpas("UPI ID है verify officer one", "Two three at OK")
    assert f.value == "verifyofficer123@OK" and f.needs_confirmation


@pytest.mark.parametrize("text, handle", [
    ("UPI ID है rajesh at OK axis", "okaxis"),
    ("UPI ID है rajesh at ok access", "okaxis"),                  # Prisma wrote "access" for "axis" in the rehearsal call
    ("UPI ID है rajesh at okay access", "okaxis"),                # and "okay" for "ok"
    ("UPI ID है rajesh at ok SBI", "oksbi"),
    ("UPI ID है rajesh at ok s b i", "oksbi"),
    ("UPI ID है rajesh at ok icici", "okicici"),
    ("UPI ID है rajesh at ok hdfc bank", "okhdfcbank"),
    ("UPI ID है rajesh at ok hdfc", "okhdfcbank"),
    ("UPI ID है rajesh at ओके एक्सिस", "okaxis"),
])
def test_spoken_variants_are_joined(text, handle):
    (f,) = vpas(text)
    assert f.value == f"rajesh@{handle}" and f.needs_confirmation and f.confidence == "heard"


def test_a_written_ok_axis_is_joined_too():
    (f,) = vpas("UPI ID है rajesh@OK axis")
    assert f.value == "rajesh@okaxis" and f.needs_confirmation and f.quote == "rajesh@OK axis"


def test_the_fragment_must_be_in_the_same_transcript_piece():
    """The rehearsal's third spelling arrived as "At" / "Okay" / "AXIX" in four pieces: not joined (and not a UPI id at all)."""
    found = vpas("UPI ID है verify officer one two three", "At okay", "axis")
    assert all(not f.value.endswith("okaxis") for f in found)


def test_an_ok_followed_by_an_ordinary_word_is_not_joined():
    (f,) = vpas("UPI ID है rajesh at okay sir")
    assert f.value == "rajesh@okay"


def test_ok_and_okay_are_the_only_ok_words():
    assert OK_WORDS == {"ok", "okay", "ओके"}


# ---------------------------------------------------------------- 2. the guess list is ranked by how well it matches

def test_okay_from_the_rehearsal_ranks_okaxis_first_and_suggests_it():
    (f,) = vpas("UPI ID है verify officer one two three at okay")           # the rehearsal's first spelling
    assert f.value == "verifyofficer123@okay" and f.needs_confirmation
    assert f.candidates[0] == "verifyofficer123@okaxis" and f.suggested == "verifyofficer123@okaxis"
    assert set(f.candidates) == {f"verifyofficer123@{h}" for h in KNOWN_UPI_PROVIDERS if h.startswith("ok")}


def test_a_bare_ok_has_no_closest_handle_so_it_is_listed_alphabetically_with_no_suggestion():
    (f,) = vpas("UPI ID है verify officer one two three at OK")
    assert f.value == "verifyofficer123@OK" and f.suggested is None
    assert list(f.candidates) == sorted(f.candidates)                      # was oksbi first, only because it is the shortest name
    assert f.candidates[0] != "verifyofficer123@oksbi"


def test_a_close_spelling_comes_before_an_arbitrary_one():
    (f,) = vpas("UPI ID है rajesh at oksb")
    assert f.candidates[0] == "rajesh@oksbi" and f.suggested == "rajesh@oksbi"
    (g,) = vpas("UPI ID है rajesh at okicic")
    assert g.candidates[0] == "rajesh@okicici"


def test_the_value_is_never_replaced_by_a_guess():
    for text in ("at okay", "at OK", "at oksb", "at okidoki"):
        (f,) = vpas("UPI ID है rajesh " + text)
        assert f.value == "rajesh@" + text.split()[-1] and f.needs_confirmation


def test_known_handles_still_need_no_confirmation():
    (f,) = vpas("UPI ID है rajesh at oksbi")
    assert f.value == "rajesh@oksbi" and not f.needs_confirmation and f.candidates == ()
