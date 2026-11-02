"""Tests for mirage/extract.py.

Most fixtures are TEXT copied from our own Prisma runs (the clip and the latency test): no audio, no raw JSON.
Anything made up for a test is labelled INVENTED. The phone and account numbers in the clip are synthetic.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import random

import pytest

from mirage.extract import (
    DIGIT_WORDS, Transcript, extract_intel, is_plausible_account, is_synthetic_phone, is_valid_ifsc,
    is_valid_vpa, looks_like_real_mobile,
)

# ---- real transcripts (text only) -------------------------------------------------------------------------------
FULL_CLIP = (
    "हाँ जी, मेरा नाम राजेश है। मेरा नंबर है ज़ीरो ज़ीरो चार सात एक तीन आठ दो नौ छह। अकाउंट नंबर है एक दो तीन चार "
    "पाँच छह सात आठ नौ ज़ीरो एक दो। UPI ID है राजेश dot test at miraj UPI। IFSC है NAI RG ज़ीरो ज़ीरो ज़ीरो ज़ीरो एक दो तीन।"
)
# The same clip cut every 6 s by x-max-speech-s (a setting we decided NOT to use); a number is split across pieces.
CUT_A = "हाँ जी, मेरा नाम राजेश है। मेरा नंबर है zero zero चार सात एक तीन आठ।"
CUT_B = "दो नौ छह account number है एक दो तीन चार पाँच छह सात आठ नौ zero"
CUT_C = "UPI ID है राजेश.test@miraj UPI"
CUT_D = "सी है N A I R G ज़ीरो ज़ीरो ज़ीरो ज़ीरो एक दो तीन।"
# The seven latency-test sentences as transcribed.
LATENCY = [
    "नमस्ते, मैं CBI officer बोल रहा हूँ।",
    "आपके नाम से एक parcel पकड़ा गया है।",
    "आपको अभी case number note करना होगा।",
    "केस नंबर है चार सात दो नौ एक।",
    "अब payment के लिए account number लिखिए।",
    "account number है एक, दो, तीन, चार, पाँच, छह, सात, आठ।",
    "जल्दी कीजिए time कम है",
]


def one(text: str, **kwargs):
    findings = extract_intel([Transcript(text, 1.0)], **kwargs)
    assert len(findings) == 1, findings
    return findings[0]


def kinds(text: str, **kwargs) -> list[str]:
    return [f.kind for f in extract_intel([Transcript(text, 1.0)], **kwargs)]


# ---------------------------------------------------------------- the real clip

def test_full_clip_gives_phone_account_upi_and_ifsc_in_order():
    findings = extract_intel([Transcript(FULL_CLIP, 24.5, 23.2)])
    assert [f.kind for f in findings] == ["phone", "account", "vpa", "ifsc"]
    phone, account, vpa, ifsc = findings

    assert phone.value == "0047138296" and phone.confidence == "weak_keyword"
    assert not phone.valid and "synthetic mode" in phone.reason       # a 00 number is not a real mobile
    assert phone.needs_confirmation

    assert account.value == "123456789012" and account.valid          # 12 digits, not 10
    assert account.confidence == "keyword" and not account.needs_confirmation

    assert vpa.value == "राजेश.test@miraj UPI"                          # as heard, nothing "corrected"
    assert not vpa.valid and vpa.needs_confirmation

    assert ifsc.value == "NAIRG0000123"                                # 5 letters heard, so 12 characters
    assert not ifsc.valid and ifsc.needs_confirmation                  # we do not "fix" it


def test_synthetic_mode_accepts_the_staged_phone_number_but_is_off_by_default():
    assert not extract_intel([Transcript(FULL_CLIP, 1.0)])[0].valid
    phone = extract_intel([Transcript(FULL_CLIP, 1.0)], synthetic_mode=True)[0]
    assert phone.valid and phone.value == "0047138296"


# ---------------------------------------------------------------- A: the heard value is never replaced

def test_the_suggestion_never_replaces_the_heard_value():
    plain = extract_intel([Transcript(FULL_CLIP, 1.0)])[2]
    assert plain.suggested is None                                    # outside synthetic mode: no guess at all

    synthetic = extract_intel([Transcript(FULL_CLIP, 1.0)], synthetic_mode=True)[2]
    assert synthetic.suggested == "राजेश.test@mirageupi"
    assert synthetic.value == "राजेश.test@miraj UPI"                    # the value is still what was heard
    assert "mirageupi" not in synthetic.value
    assert synthetic.value != synthetic.suggested


def test_a_written_upi_id_keeps_exactly_the_heard_text():
    f = one(CUT_C, synthetic_mode=True)
    assert f.kind == "vpa" and f.value == f.quote == "राजेश.test@miraj UPI"
    assert f.suggested == "राजेश.test@mirageupi"


def test_the_invented_provider_is_recognised_only_in_synthetic_mode():
    # INVENTED test string
    text = "UPI ID rajesh at mirageupi"
    assert one(text).needs_confirmation and "known list" in one(text).reason
    synthetic = one(text, synthetic_mode=True)
    assert synthetic.valid and not synthetic.needs_confirmation


# ---------------------------------------------------------------- B: provenance

ALL_FIXTURES = [
    [Transcript(FULL_CLIP, 24.5, 23.2)],
    [Transcript(CUT_A, 6.66, 6.0), Transcript(CUT_B, 12.67, 6.0), Transcript(CUT_C, 18.62, 6.0), Transcript(CUT_D, 23.98, 6.0)],
    [Transcript(t, 4.0 * i + 1.0, 2.0) for i, t in enumerate(LATENCY)],
]


@pytest.mark.parametrize("transcripts", ALL_FIXTURES)
@pytest.mark.parametrize("mode", [False, True])
def test_every_finding_has_an_exact_quote_a_time_and_a_source(transcripts, mode):
    findings = extract_intel(transcripts, synthetic_mode=mode)
    assert findings
    for f in findings:
        assert f.quote.strip()
        parts = f.quote.split(" | ")
        first = transcripts[f.source_index]
        assert f.t == first.t
        assert parts[0] in first.text
        for part in parts[1:]:
            assert any(part in tr.text for tr in transcripts[f.source_index + 1:])


def test_a_number_split_across_two_transcripts_is_quoted_from_both():
    a, b = Transcript(CUT_A, 6.66, 6.0), Transcript(CUT_B, 12.67, 6.0)
    phone = extract_intel([a, b])[0]
    assert phone.value == "0047138296"
    assert phone.source_index == 0 and phone.t == 6.66
    assert phone.quote == "zero zero चार सात एक तीन आठ | दो नौ छह"        # the digits only, one slice per transcript


# ---------------------------------------------------------------- joining across transcripts

def test_the_cut_clip_is_joined_into_the_same_phone_and_account():
    findings = extract_intel([Transcript(CUT_A, 6.66, 6.0), Transcript(CUT_B, 12.67, 6.0)])
    phone, account = findings
    assert (phone.kind, phone.value) == ("phone", "0047138296")
    assert (account.kind, account.value, account.valid) == ("account", "1234567890", True)


def test_transcripts_too_far_apart_are_not_joined():
    far = extract_intel([Transcript(CUT_A, 6.66, 6.0), Transcript(CUT_B, 40.0, 1.0)])
    assert "0047138296" not in [f.value for f in far]
    assert far[0].value == "0047138"


def test_the_window_depends_on_when_the_next_speech_began_not_only_when_it_arrived():
    # INVENTED timings. B arrives 8 s after A but covers 6 s of speech, so it began about 2 s after A arrived.
    close = extract_intel([Transcript("मेरा नंबर है एक दो तीन।", 10.0, 2.0), Transcript("चार पाँच छह", 18.0, 6.0)])
    assert [f.value for f in close] == ["123456"]
    apart = extract_intel([Transcript("मेरा नंबर है एक दो तीन।", 10.0, 2.0), Transcript("चार पाँच छह", 18.0, 1.0)])
    assert [f.value for f in apart] == ["123", "456"]


def test_a_keyword_at_the_end_of_one_transcript_applies_to_digits_in_the_next():
    findings = extract_intel([Transcript("मेरा account number है", 5.0, 2.0),
                              Transcript("एक दो तीन चार पाँच छह सात आठ नौ", 6.0, 2.0)])
    assert [(f.kind, f.value, f.confidence) for f in findings] == [("account", "123456789", "keyword")]


# ---------------------------------------------------------------- real latency fixtures

def test_the_latency_sentences_give_one_case_number_and_one_account():
    findings = extract_intel([Transcript(t, float(i)) for i, t in enumerate(LATENCY)])
    assert [(f.kind, f.value) for f in findings] == [("case", "47291"), ("account", "12345678")]
    assert findings[0].confidence == "keyword" and not findings[0].needs_confirmation
    assert not findings[1].valid and "9 to 18" in findings[1].reason     # 8 digits is too short for an account


@pytest.mark.parametrize("text", [
    LATENCY[0], LATENCY[1], LATENCY[2], LATENCY[4], LATENCY[6],
    "नमस्ते।",
    # INVENTED ordinary sentences with words that can also be digits ("एक" a/one, "दो" give, "do")
    "मुझे एक गिलास पानी दो", "what do you do for work", "एक दो मिनट रुकिए", "दो बार बताइए बेटा",
])
def test_ordinary_sentences_produce_no_numbers(text):
    assert kinds(text) == []


# ---------------------------------------------------------------- digit words, numerals, separators

@pytest.mark.parametrize("digit, word", [(d, w) for w, d in DIGIT_WORDS.items()])
def test_every_digit_word_reads_as_its_digit(digit, word):
    assert one(f"{word} {word} {word}").value == digit * 3


def test_both_spellings_of_zero_and_the_extra_zero_words():
    for word in ("ज़ीरो", "जीरो", "शून्य", "सिफ़र", "सिफर", "zero"):
        assert one(f"{word} चार सात").value == "047"


def test_oh_and_devanagari_o_are_not_zero():
    assert kinds("oh oh चार सात") == []                 # only two real digits: below the run length
    assert [f.value for f in extract_intel([Transcript("ओ चार सात एक", 1.0)])] == ["471"]


def test_mixed_hindi_and_english_digit_words():
    assert one("zero zero चार सात एक three आठ").value == "0047138"


def test_ascii_and_devanagari_numerals():
    assert one("0047138296").value == "0047138296"
    assert one("००४७१३८२९६").value == "0047138296"
    assert one("०० ४७ १३८").value == "0047138"


def test_commas_and_hyphens_do_not_split_a_number_but_a_sentence_end_does():
    assert one("एक, दो, तीन, चार").value == "1234"
    assert one("चार-सात-दो").value == "472"
    assert [f.value for f in extract_intel([Transcript("एक दो तीन। चार पाँच छह", 1.0)])] == ["123", "456"]


def test_double_and_triple_INVENTED_strings():
    assert one("double zero चार").value == "004"
    assert one("डबल सात triple नौ").value == "77999"
    assert one("ट्रिपल ज़ीरो एक").value == "0001"


def test_run_length_is_configurable():
    assert kinds("एक दो") == []
    assert one("एक दो", min_run=2).value == "12"
    assert one("पाँच", min_run=1).value == "5"


def test_a_digit_may_be_missing_when_one_unknown_word_sits_between_two_runs():
    findings = extract_intel([Transcript("एक दो तीन foo चार पाँच छह", 1.0)])
    assert [f.value for f in findings] == ["123", "456"]
    assert all(f.needs_confirmation and "digit may be missing" in f.reason for f in findings)


# ---------------------------------------------------------------- which number is it?

def test_ten_digits_without_a_keyword_are_ambiguous():
    f = one("एक दो तीन चार पाँच छह सात आठ नौ ज़ीरो")
    assert f.kind == "ambiguous" and f.candidates == ("phone", "account") and f.needs_confirmation


def test_the_expected_hint_breaks_a_tie_and_lowers_confidence():
    text = "एक दो तीन चार पाँच छह सात आठ नौ ज़ीरो"
    f = one(text, expected="account")
    assert (f.kind, f.confidence, f.needs_confirmation) == ("account", "expected", True)
    assert one(text, expected="phone").kind == "phone"


def test_a_specific_keyword_beats_the_expected_hint():
    f = one("account number है एक दो तीन चार पाँच छह सात आठ नौ ज़ीरो", expected="phone")
    assert (f.kind, f.confidence) == ("account", "keyword")


def test_the_bare_word_number_loses_to_the_expected_hint():
    digits = "एक दो तीन चार पाँच छह सात आठ नौ ज़ीरो एक दो"
    assert one(f"नंबर है {digits}").kind == "phone"
    assert one(f"नंबर है {digits}", expected="account").kind == "account"


def test_a_hint_that_does_not_fit_is_ignored():
    assert one("एक दो तीन चार पाँच", expected="phone").kind == "ambiguous"


def test_with_no_keyword_length_is_the_last_resort():
    assert one("एक दो तीन चार पाँच छह सात आठ नौ ज़ीरो एक दो").kind == "account"
    assert one("एक दो तीन चार").kind == "ambiguous"


def test_an_unknown_expected_value_is_refused():
    with pytest.raises(ValueError):
        extract_intel([Transcript("x", 0.0)], expected="aadhaar")


# ---------------------------------------------------------------- letters and IFSC

def test_spelled_letters():
    assert one("N A I R G").value == "NAIRG" and one("N A I R G").needs_confirmation
    assert one("एम आई आर जी").value == "MIRG"
    assert kinds("I have a big car") == []                # single words "I" and "a" are not a spelled run


def test_ifsc_is_assembled_from_letters_and_digits_and_checked_but_never_fixed():
    spelled = one("आई एफ एस सी है M I R G ज़ीरो ज़ीरो ज़ीरो ज़ीरो एक दो तीन")
    assert (spelled.kind, spelled.value, spelled.valid) == ("ifsc", "MIRG0000123", True)
    heard = extract_intel([Transcript(FULL_CLIP, 1.0)])[-1]
    assert heard.value == "NAIRG0000123" and not heard.valid and heard.suggested is None


def test_when_the_word_ifsc_is_garbled_no_ifsc_is_invented():
    assert kinds(CUT_D) == ["letters", "ambiguous"]


# ---------------------------------------------------------------- UPI ids

def test_spoken_upi_ids():
    assert one("UPI ID है rajesh dot test at the rate ybl").value == "rajesh.test@ybl"
    ok = one("UPI ID है rajesh dot test at ybl")
    assert ok.valid and not ok.needs_confirmation
    assert one("UPI ID है rajesh nau saat at ybl").value == "rajesh97@ybl"       # digit words inside the name
    assert one("यूपीआई आईडी है rajesh underscore kumar एट द रेट okaxis").value == "rajesh_kumar@okaxis"


def test_an_ordinary_at_is_not_a_upi_id():
    assert kinds("I am at home") == []
    assert kinds("rajesh dot test at ybl") == []                 # no UPI word nearby


def test_a_hindi_name_stays_as_heard_and_is_flagged():
    f = one("UPI ID है राजेश dot test at ybl")
    assert f.value == "राजेश.test@ybl" and not f.valid and f.needs_confirmation


def test_a_literal_ascii_upi_id():
    f = one("मेरा UPI rajesh.test@ybl है")
    assert f.value == "rajesh.test@ybl" and f.valid and not f.needs_confirmation


# ---------------------------------------------------------------- validators

def test_real_mobile_check():
    assert looks_like_real_mobile("9876543210")
    assert looks_like_real_mobile("919876543210") and looks_like_real_mobile("09876543210")
    for bad in ("5876543210", "0047138296", "98765432", "98765432100", "98765abcde", "९८७६५४३२१०", ""):
        assert not looks_like_real_mobile(bad)


def test_the_gate_check_has_no_mode_parameter_and_synthetic_mode_cannot_weaken_it():
    assert list(inspect.signature(looks_like_real_mobile).parameters) == ["digits"]
    assert is_synthetic_phone("0047138296") and not is_synthetic_phone("9876543210")
    extract_intel([Transcript(FULL_CLIP, 1.0)], synthetic_mode=True)
    assert not looks_like_real_mobile("0047138296")


def test_ifsc_account_and_vpa_validators():
    assert is_valid_ifsc("MIRG0000123") and not is_valid_ifsc("NAIRG0000123") and not is_valid_ifsc("mirg0000123")
    assert [is_plausible_account("1" * n) for n in (8, 9, 18, 19)] == [False, True, True, False]
    assert is_valid_vpa("rajesh.test@ybl") and not is_valid_vpa("राजेश.test@ybl") and not is_valid_vpa("a@b c")


# ---------------------------------------------------------------- strings, leading zeros, random round trips

def test_values_are_strings_and_leading_zeros_survive_a_json_round_trip():
    for f in extract_intel([Transcript(FULL_CLIP, 1.0)], synthetic_mode=True):
        assert isinstance(f.value, str)
    phone = extract_intel([Transcript(FULL_CLIP, 1.0)])[0]
    back = json.loads(json.dumps(dataclasses.asdict(phone), ensure_ascii=False))
    assert back["value"] == "0047138296"


WORDS = {
    "devanagari": ["ज़ीरो", "एक", "दो", "तीन", "चार", "पाँच", "छह", "सात", "आठ", "नौ"],
    "english": ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"],
    "hinglish": ["jeero", "ek", "do", "teen", "chaar", "paanch", "chhe", "saat", "aath", "nau"],
}
DEVANAGARI_NUMERALS = "०१२३४५६७८९"


def render(digits: str, rng: random.Random) -> list[str]:
    """INVENTED: spell a digit string as a random mix of words and numeral chunks, as a list of pieces."""
    pieces, i = [], 0
    while i < len(digits):
        style = rng.choice(["devanagari", "english", "hinglish", "ascii", "deva_num"])
        if style in ("ascii", "deva_num"):
            chunk = digits[i:i + rng.randint(1, 4)]
            pieces.append(chunk if style == "ascii" else "".join(DEVANAGARI_NUMERALS[int(c)] for c in chunk))
            i += len(chunk)
        else:
            pieces.append(WORDS[style][int(digits[i])])
            i += 1
    return pieces


def test_random_digit_strings_round_trip_exactly():
    rng = random.Random(12345)
    for _ in range(200):
        digits = "".join(rng.choice("0123456789") for _ in range(rng.randint(3, 18)))
        pieces = render(digits, rng)
        text = "".join(p + rng.choice([" ", " ", ", ", "-"]) for p in pieces).rstrip(" ,-")
        f = one(text)
        assert f.value == digits, (text, digits, f.value)


def test_random_digit_strings_split_across_two_transcripts_join_back():
    rng = random.Random(54321)
    for _ in range(100):
        digits = "".join(rng.choice("0123456789") for _ in range(rng.randint(4, 18)))
        pieces = render(digits, rng)
        if len(pieces) < 2:
            continue
        cut = rng.randint(1, len(pieces) - 1)
        a, b = " ".join(pieces[:cut]) + "।", " ".join(pieces[cut:])
        findings = extract_intel([Transcript(a, 10.0, 1.0), Transcript(b, 11.0, 1.0)])
        assert [f.value for f in findings] == [digits], (a, b)
        assert findings[0].source_index == 0 and " | " in findings[0].quote



# ---------------------------------------------------------------- amounts of money are not identifiers

@pytest.mark.parametrize("text, value", [
    ("Aapko 500 rupaye verification ke liye transfer karne hain", "500"),     # from the owner's practice call
    ("Rs 5000 bhejiye", "5000"), ("Rs. 5000 bhejiye", "5000"), ("INR 2500 jama kijiye", "2500"),
    ("₹ 2500 jama kijiye", "2500"), ("₹2500", "2500"),
    ("aapko do lakh rupaye bhejne honge", "2"), ("paanch hazaar dena hoga", "5"),
    ("५०० रुपये", "500"), ("ek crore", "1"),
])
def test_a_number_next_to_a_money_word_is_an_amount_with_quote_and_time(text, value):
    findings = extract_intel([Transcript(text, 7.5)])
    assert [(f.kind, f.value) for f in findings] == [("amount", value)]
    f = findings[0]
    assert f.valid and not f.needs_confirmation and f.quote and f.t == 7.5 and f.source_index == 0


def test_a_bare_number_is_still_ambiguous_and_money_words_alone_are_nothing():
    assert one("500").kind == "ambiguous"
    assert kinds("sau rupaye ka kaam hai") == []
    assert kinds("account number hai 500") == ["account"]


def test_an_amount_beside_a_real_identifier_does_not_swallow_it():
    findings = extract_intel([Transcript("account number hai ek do teen chaar paanch chhe saat aath nau, Rs 500 bhejiye", 1.0)])
    assert [(f.kind, f.value) for f in findings] == [("account", "123456789"), ("amount", "500")]
