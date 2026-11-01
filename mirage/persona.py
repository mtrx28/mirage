"""Rajesh Uncle's fact sheet, loaded from content/persona.json and checked on load.

Everything in the sheet is invented. The checks here exist so that an edit cannot quietly put something
real-looking in it (for example a phone number that starts with 6 to 9).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from mirage.config import ROOT
from mirage.extract import is_plausible_account, is_synthetic_phone, is_valid_ifsc, looks_like_real_mobile

PERSONA_PATH = ROOT / "content" / "persona.json"
FORBIDDEN_FACT_NAMES = {"aadhaar", "aadhar", "pan", "otp", "pin", "password", "cvv", "card", "card_number"}


@dataclass(frozen=True)
class Fact:
    key: str
    label: str      # what Uncle calls it when speaking ("account number")
    value: str      # kept as a string so leading zeros survive
    kind: str       # text | digits | code


@dataclass(frozen=True)
class Persona:
    facts: dict[str, Fact]
    character: str

    def fact(self, key: str) -> Fact:
        return self.facts[key]


def load_persona(path: Path = PERSONA_PATH) -> Persona:
    raw = json.loads(path.read_text(encoding="utf-8"))
    facts = {
        key: Fact(key, f["label"], f["value"], f["kind"])
        for key, f in raw["facts"].items()
    }
    return Persona(facts, raw.get("character", ""))


def check_persona(path: Path = PERSONA_PATH) -> list[str]:
    """Return a list of problems; an empty list means the sheet is safe to use."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if raw.get("fictional") is not True:
        problems.append('top-level "fictional" must be true')
    for key, f in raw["facts"].items():
        value, kind = f.get("value"), f.get("kind")
        if not isinstance(value, str) or not value:
            problems.append(f"{key}: value must be a non-empty string (strings keep leading zeros)")
            continue
        if key.lower() in FORBIDDEN_FACT_NAMES:
            problems.append(f"{key}: this kind of fact must never be in the sheet")
        if kind not in ("text", "digits", "code"):
            problems.append(f"{key}: kind must be text, digits or code")
        if kind == "digits" and not (value.isascii() and value.isdigit()):
            problems.append(f"{key}: digits fact must be ASCII digits only")
        if kind == "digits" and looks_like_real_mobile(value):
            problems.append(f"{key}: looks like a real Indian mobile number")
        if key == "phone" and not is_synthetic_phone(value):
            problems.append("phone: must follow the synthetic convention (10 digits starting 00)")
        if key == "phone" and not value.startswith("0000"):
            problems.append("phone: new synthetic numbers must start 0000, so they cannot read as +<country code> "
                            "(reasoning unverified; the older 00xx clip numbers are only test fixtures)")
        if key == "account" and not is_plausible_account(value):
            problems.append("account: must be 9 to 18 digits")
        if key == "account" and len(value) == 12:
            problems.append("account: 12 digits looks like an Aadhaar-style number; use another length")
        if key == "ifsc":
            if not is_valid_ifsc(value):
                problems.append("ifsc: not in IFSC format")
            if "unverified" not in f:
                problems.append("ifsc: must carry an 'unverified' note until the bank code is checked against RBI's list")
    return problems
