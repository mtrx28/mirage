"""Tests for the persona fact sheet and the template lines (piece 1 of the brain).

Half of these prove the real files pass. The other half plant a bad line or a bad fact and check the checker
CATCHES it, because a checker that never fails proves nothing.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from mirage.config import ROOT
from mirage.lines import (
    ALLOWED_TAGS, Line, LineBank, MAX_CHARS, MIN_CHARS, load_lines, lint, measured_text, review_table,
)
from mirage.persona import check_persona, load_persona

PERSONA_FILE = ROOT / "content" / "persona.json"
RAW_PERSONA = json.loads(PERSONA_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- the real files

def test_the_real_persona_sheet_passes_its_checks():
    assert check_persona() == []


def test_the_real_lines_pass_every_rule():
    assert lint(load_lines()) == []


def test_the_counts_match_the_plan():
    bank = load_lines()
    assert 40 <= len(bank.lines) <= 60
    for tactic in bank.tactics:
        assert 4 <= len(bank.tactic_lines(tactic)) <= 6, tactic
    assert sum(bool(ln.tags) for ln in bank.lines) <= 12
    assert {t for ln in bank.lines for t in ln.tags} <= ALLOWED_TAGS


def test_ek_minute_is_fine_even_though_ek_is_a_digit_word():
    bank = load_lines()
    assert any("ek minute" in ln.text.lower() for ln in bank.lines)
    assert lint(bank) == []


def test_persona_is_invented_and_cannot_look_like_a_real_mobile_number():
    persona = load_persona()
    assert RAW_PERSONA["fictional"] is True
    assert persona.fact("phone").value.startswith("0000")
    assert isinstance(persona.fact("phone").value, str) and len(persona.fact("phone").value) == 10
    assert "unverified" in RAW_PERSONA["facts"]["ifsc"]


def test_uncle_does_not_say_beta_in_every_line():
    bank = load_lines()
    with_beta = sum("beta" in ln.text.lower() for ln in bank.lines)
    assert with_beta <= len(bank.lines) / 3


def test_obstacle_tactics_carry_their_trigger():
    bank = load_lines()
    assert bank.triggers == {
        "give_own_detail": "asked_for_own_detail",
        "app_not_opening": "asked_to_install_app",
        "otp_late": "asked_for_otp",
        "limit_exceeded_second_account": "asked_for_payment",
    }


def test_the_word_darja_is_gone_and_post_is_used():
    texts = " ".join(ln.text.lower() for ln in load_lines().lines)
    assert "darja" not in texts and " post " in f" {texts} "


def test_the_review_table_has_one_row_per_line():
    bank = load_lines()
    table = review_table(bank).splitlines()
    assert len(table) == len(bank.lines) + 2
    assert all(MIN_CHARS <= len(measured_text(ln, bank, load_persona())) <= MAX_CHARS for ln in bank.lines)


# ---------------------------------------------------------------- the checker catches planted bad lines

def with_extra_line(text: str, tactic: str = "generic_stall") -> tuple[LineBank, Line]:
    bank = load_lines()
    states = bank.tactic_lines(tactic)[0].states
    bad = Line("planted_01", tactic, states, "any", text, ())
    return dataclasses.replace(bank, lines=bank.lines + (bad,)), bad


def problems_for(text: str, tactic: str = "generic_stall") -> list[str]:
    bank, bad = with_extra_line(text, tactic)
    return [p for p in lint(bank) if p.startswith(bad.id)]


@pytest.mark.parametrize("text, expected_fragment", [
    ("Mera number hai 9876543210, likh lijiye beta, theek hai?", "numeral"),
    ("Mera number hai ek do teen chaar paanch, likh lijiye beta.", "extract_intel finds"),
    ("Paise bhejne ke baad UPI rajesh@ybl par likh lo beta, theek hai.", "'@'"),
    ("Main insaan hoon beta, koi machine nahin, tum chinta mat karo.", "person, human, robot or AI"),
    ("Arre beta, main koi robot nahin hoon, asli aadmi hoon main.", "person, human, robot or AI"),
    ("Aapka OTP bataiye beta, phir main paise bhej dunga turant.", "money or credentials"),
    ("Mujhe paise bhej do beta, phir main aapko baad mein bataunga.", "money or credentials"),
    ("Maine paise bhej diye beta, ab aap dekh lo kitne aaye hain.", "already sent"),
    ("Maine OTP bata diya hai beta, ab aap check kar lo na jaldi.", "already sent"),
    ("Ok.", "characters once slots are filled"),
    ("Haan beta, main is baat ko samajhne ki koshish kar raha hoon, ek baar aur dheere dheere samjhao, theek hai?",
     "characters once slots are filled"),
    ("<coughs> <sigh> Haan beta, ek minute ruko, abhi aata hoon main.", "more than one tag"),
    ("<laughs> Haan beta, ek minute ruko, abhi aata hoon main yahin.", "is not one of"),
    ("Haan beta, {nonsense} ek minute ruko, abhi aata hoon main yahin.", "unknown slot"),
    ("हाँ बेटा, एक मिनट रुको, अभी आता हूँ मैं यहीं पर ही।", "ASCII"),
    ("Dekho beta, http://example.com par jaake dekh lo, theek hai?", "'@', a link"),
    ("Aap boliye na, tum jaldi se bata do mujhe sab kuch aaj hi.", "informal form"),
    ("Achha, ab suno beta, main ek baar phir se likh leta hoon.", "informal form"),
])
def test_the_checker_catches_a_planted_bad_line(text, expected_fragment):
    problems = problems_for(text)
    assert any(expected_fragment in p for p in problems), problems


def test_a_digit_slot_in_the_wrong_tactic_is_caught():
    problems = problems_for("Haan beta, ye likha {readback} theek hai na beta?")
    assert any("not allowed in generic_stall" in p for p in problems)


def test_a_tactic_line_missing_its_required_slot_is_caught():
    problems = problems_for("Zara dheere boliye beta, main likh raha hoon, ruko.", "ask_detail_slow")
    assert any("must contain {what}" in p for p in problems)


def test_too_many_tagged_lines_are_caught():
    bank = load_lines()
    tagged = tuple(dataclasses.replace(ln, text="<sigh> " + ln.text) for ln in bank.lines)
    assert any("carry a tag" in p for p in lint(dataclasses.replace(bank, lines=tagged)))


def test_too_many_beta_lines_are_caught():
    bank = load_lines()
    betas = tuple(dataclasses.replace(ln, text=ln.text.replace("Haan", "Haan beta,", 1)) if "Haan" in ln.text else ln
                  for ln in bank.lines)
    more = tuple(dataclasses.replace(ln, text=ln.text + " beta") if "beta" not in ln.text else ln for ln in betas[:30]) + betas[30:]
    assert any("say 'beta'" in p for p in lint(dataclasses.replace(bank, lines=more)))


def test_an_obstacle_tactic_without_its_trigger_is_caught():
    bank = load_lines()
    no_trigger = dataclasses.replace(bank, triggers={k: v for k, v in bank.triggers.items() if k != "otp_late"})
    assert any("otp_late: must have trigger" in p for p in lint(no_trigger))
    wrong = dataclasses.replace(bank, triggers={**bank.triggers, "otp_late": "made_up"})
    assert any("unknown trigger" in p for p in lint(wrong))


def test_a_tactic_with_too_few_variants_is_caught():
    bank = load_lines()
    kept = tuple(ln for ln in bank.lines if ln.id != "generic_stall_04")
    assert any("generic_stall: 3 variants" in p for p in lint(dataclasses.replace(bank, lines=kept)))


# ---------------------------------------------------------------- the checker catches a bad persona sheet

def checked(tmp_path, mutate) -> list[str]:
    raw = json.loads(json.dumps(RAW_PERSONA))
    mutate(raw)
    path = tmp_path / "persona.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return check_persona(path)


def test_a_real_looking_phone_number_is_refused(tmp_path):
    problems = checked(tmp_path, lambda raw: raw["facts"]["phone"].update(value="9876543210"))
    assert any("real Indian mobile" in p for p in problems)
    assert any("synthetic convention" in p for p in problems)


def test_a_new_phone_number_must_start_with_four_zeros(tmp_path):
    problems = checked(tmp_path, lambda raw: raw["facts"]["phone"].update(value="0038274915"))
    assert any("must start 0000" in p for p in problems)
    assert checked(tmp_path, lambda raw: raw["facts"]["phone"].update(value="0000274915")) == []


def test_a_twelve_digit_account_is_refused(tmp_path):
    problems = checked(tmp_path, lambda raw: raw["facts"]["account"].update(value="123456789012"))
    assert any("Aadhaar" in p for p in problems)


def test_an_aadhaar_or_otp_fact_is_refused(tmp_path):
    def add(raw):
        raw["facts"]["aadhaar"] = {"label": "aadhaar", "value": "x", "kind": "text"}
    assert any("never be in the sheet" in p for p in checked(tmp_path, add))


def test_the_ifsc_must_keep_its_unverified_note_and_the_sheet_must_say_fictional(tmp_path):
    assert any("unverified" in p for p in checked(tmp_path, lambda raw: raw["facts"]["ifsc"].pop("unverified")))
    assert any("fictional" in p for p in checked(tmp_path, lambda raw: raw.update(fictional=False)))
