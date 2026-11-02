"""How Uncle SAYS digits and codes. Only used to build the text of a reply; the digits themselves always come
from the persona fact sheet or from the caller's own findings (see fsm.py).

Timbre's text-normalisation guide (docs.gnani.ai/api/TTS/tts-input-formating.md) says it reads bare digit runs
digit by digit near words like "account", reads a 10-digit number in groups of 3-3-4, and always spells
IFSC, OTP, UPI and KYC letter by letter. It does NOT document what happens to number WORDS in the input
("ek do teen"). The owner listened to four versions for Prakash (digit words, spaced numerals, plain numerals,
Devanagari digit words) and chose spaced numerals, "0 0 4, 7 1 3": a single sample of each version, one listener.
"""
from __future__ import annotations

import re

from mirage.persona import Fact

DIGIT_WORDS_SPOKEN = {"0": "zero", "1": "ek", "2": "do", "3": "teen", "4": "chaar",
                      "5": "paanch", "6": "chhe", "7": "saat", "8": "aath", "9": "nau"}
DIGIT_STYLES = ("spaced", "words")
# After listening to all four versions the owner chose spaced numerals ("0 0 4, 7 1 3"): one sample per version, one
# listener, one voice. FSMConfig.digit_style has the same default, and a test keeps the two equal.
DEFAULT_DIGIT_STYLE = "spaced"


def _check_style(style: str) -> None:
    if style not in DIGIT_STYLES:
        raise ValueError(f"digit style must be one of {DIGIT_STYLES}, not {style!r}")


def _groups(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def speak_digits(digits: str, *, group: int = 3, style: str = DEFAULT_DIGIT_STYLE) -> str:
    """"0047138" -> "0 0 4, 7 1 3, 8" (spaced) or "zero zero chaar, saat ek teen, aath" (words): digits in small
    groups, a comma (a short pause) between."""
    _check_style(style)
    if not (digits.isascii() and digits.isdigit()):
        raise ValueError("speak_digits takes ASCII digits only")
    pieces = [DIGIT_WORDS_SPOKEN[d] if style == "words" else d for d in digits]
    return ", ".join(" ".join(g) for g in _groups(pieces, group))


def speak_code(chars: str, *, group: int = 3, style: str = DEFAULT_DIGIT_STYLE) -> str:
    """A code with letters and digits, e.g. "MIRG0000" -> "M I R, G 0 0, 0 0": letters and digits spelled one by one."""
    _check_style(style)
    pieces = [DIGIT_WORDS_SPOKEN[c] if (c.isdigit() and style == "words") else c.upper() for c in chars]
    return ", ".join(" ".join(g) for g in _groups(pieces, group))


def render_fact(fact: Fact, *, group: int = 3, style: str = DEFAULT_DIGIT_STYLE) -> tuple[str, list[tuple[str, str]]]:
    """Spoken form of a fact-sheet value, plus the digit strings in it as (digits, spoken text) pairs."""
    if fact.kind == "digits":
        spoken = speak_digits(fact.value, group=group, style=style)
        return spoken, [(fact.value, spoken)]
    if fact.kind == "code":
        spoken = speak_code(fact.value, group=group, style=style)
        digits = "".join(c for c in fact.value if c.isdigit())
        return spoken, ([(digits, spoken)] if digits else [])
    pairs: list[tuple[str, str]] = []

    def swap(match: re.Match) -> str:     # numerals inside a text fact (a flat number) are spoken as words too
        spoken = speak_digits(match.group(), group=group, style=style)
        pairs.append((match.group(), spoken))
        return spoken

    return re.sub(r"\d+", swap, fact.value), pairs
