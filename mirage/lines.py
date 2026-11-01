"""Rajesh Uncle's template lines: load them from content/lines.json and check them.

The lines are data, not code, so you can edit content/lines.json freely. After editing, run

    python -m mirage.lines

to see the review table and any rule that a line breaks. The same checks run as tests.
Nothing here calls any API.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from mirage.config import ROOT
from mirage.extract import Transcript, extract_intel
from mirage.persona import PERSONA_PATH, Persona, load_persona

LINES_PATH = ROOT / "content" / "lines.json"

STATES = {"S0", "S1", "S2", "S3", "S4", "S5", "any"}
SCENARIOS = {"any", "digital_arrest", "bank_kyc"}
ALLOWED_TAGS = {"<coughs>", "<pauses>", "<sigh>"}
SLOT_NAMES = {"what", "readback", "fact_label", "fact_value"}
# tactic -> (slots it may use, slots every line of it must use)
SLOT_RULES: dict[str, tuple[set[str], set[str]]] = {
    "ask_detail_slow": ({"what"}, {"what"}),
    "readback_partial": ({"readback", "what"}, {"readback"}),
    "give_own_detail": ({"fact_label", "fact_value"}, {"fact_label", "fact_value"}),
}

# Some tactics only make sense after the caller did something. The FSM will fire them only on that trigger,
# so a line like "the app will not open" is never said when nobody mentioned an app.
TRIGGERS = {"asked_to_install_app", "asked_for_otp", "asked_for_payment", "asked_for_own_detail"}
REQUIRED_TRIGGER = {
    "app_not_opening": "asked_to_install_app",
    "otp_late": "asked_for_otp",
    "limit_exceeded_second_account": "asked_for_payment",
    "give_own_detail": "asked_for_own_detail",
}
MAX_BETA_FRACTION = 1 / 3   # "beta" in every line sounds like a script; a third of the lines at most

MIN_LINES, MAX_LINES = 40, 60
MIN_VARIANTS, MAX_VARIANTS = 4, 6
MIN_CHARS, MAX_CHARS = 40, 80
MAX_TAGGED_LINES = 12       # tags are billed as characters and sound unnatural in quantity
# Stand-ins used to measure a line's length once its slots are filled.
READBACK_STANDIN = "x" * 24        # about "zero zero chaar, saat ek"
FACT_VALUE_STANDIN = "x" * 12      # digit facts are longer when spoken; this is a rough allowance

_SLOT_RE = re.compile(r"\{(\w+)\}")
_TAG_RE = re.compile(r"<[^<>]*>")

# --- safety nets (not a substitute for reading every line) -----------------------------------------
# A line must never claim Uncle is a person, or talk about robots or AI at all: the answer to a sincere
# "are you a robot?" belongs to the safety gate, not to a template.
IDENTITY_WORDS = re.compile(
    r"\b(insaan|insan|human|person|robot|bot|machine|ai|artificial|computer|program|asli|real)\b", re.IGNORECASE)
# Uncle never asks the caller for money or credentials.
ASKS_FOR_CREDENTIALS = re.compile(
    r"\b(aapka|aap ka|aapke|apna|apne)\b[^.?!]*\b(otp|password|pin|cvv|aadhaar|aadhar|pan)\b"
    r"|\b(otp|password|pin|cvv|aadhaar|aadhar)\b[^.?!]*\b(bataiye|batao|bolo|boliye|bhejiye|bhejo|dijiye|do)\b",
    re.IGNORECASE)
ASKS_FOR_MONEY = re.compile(
    r"\b(paise|paisa|rupaye|raqam|money)\b[^.?!]*\b(bhejiye|bhejo|dijiye|do|dena)\b"
    r"|\b(mujhe|mujhko|humein)\b[^.?!]*\b(paise|rupaye|raqam)\b",
    re.IGNORECASE)
# Uncle never says he ALREADY sent money, installed, clicked or shared a code. He only talks about doing it.
CLAIMS_AN_ACTION = re.compile(
    r"\b(bhej diya|bhej diye|bhej di|bhej chuka|install kar liya|install ho gaya|download kar liya|download ho gaya|"
    r"click kar diya|link khol diya|bata diya|share kar diya|transfer kar diya|pay kar diya|payment kar diya|"
    r"daal diya|dal diya|de diya)\b"
    r"|\bmaine\b[^.?!]*\b(diya|diye|di|liya|liye|chuka|chuke|chuki)\b",
    re.IGNORECASE)
# Register, decided on purpose: Uncle uses polite forms (aap, boliye, bataiye, dijiye) to everyone, because the
# caller claims to be an officer. "beta" is the only familiar word, used sparingly. So the informal forms are out.
INFORMAL_FORMS = re.compile(
    r"\b(bolo|batao|suno|dekho|karo|ruko|jao|aao|kahoge|tum|tumhara|tumhare|tumhein|likh lo|bata do|likh lijiye do)\b",
    re.IGNORECASE)
LINK_OR_HANDLE = re.compile(r"@|https?:|www\.|\.com\b", re.IGNORECASE)


@dataclass(frozen=True)
class Line:
    id: str
    tactic: str
    states: tuple[str, ...]
    scenario: str
    text: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class SlotOption:
    key: str
    text: str
    expected: str | None


@dataclass(frozen=True)
class LineBank:
    lines: tuple[Line, ...]
    what_options: tuple[SlotOption, ...]
    goals: dict[str, str]
    triggers: dict[str, str]    # tactic -> what the caller must have done first (only some tactics have one)

    def tactic_lines(self, tactic: str) -> list[Line]:
        return [ln for ln in self.lines if ln.tactic == tactic]

    @property
    def tactics(self) -> list[str]:
        return list(dict.fromkeys(ln.tactic for ln in self.lines))


def load_lines(path: Path = LINES_PATH) -> LineBank:
    raw = json.loads(path.read_text(encoding="utf-8"))
    lines: list[Line] = []
    goals: dict[str, str] = {}
    triggers: dict[str, str] = {}
    for tactic, spec in raw["tactics"].items():
        goals[tactic] = spec.get("goal", "")
        if spec.get("trigger"):
            triggers[tactic] = spec["trigger"]
        for i, variant in enumerate(spec["variants"], 1):
            scenario, text = ("any", variant) if isinstance(variant, str) else (variant["scenario"], variant["text"])
            lines.append(Line(f"{tactic}_{i:02d}", tactic, tuple(spec["states"]), scenario, text,
                              tuple(_TAG_RE.findall(text))))
    what = tuple(SlotOption(o["key"], o["text"], o.get("expected")) for o in raw["slots"]["what"])
    return LineBank(tuple(lines), what, goals, triggers)


def measured_text(line: Line, bank: LineBank, persona: Persona) -> str:
    """The line with every slot filled by a stand-in of realistic length (the longest option for word slots)."""
    longest_what = max((o.text for o in bank.what_options), key=len)
    longest_label = max((f.label for f in persona.facts.values()), key=len)
    fill = {"what": longest_what, "readback": READBACK_STANDIN, "fact_label": longest_label,
            "fact_value": FACT_VALUE_STANDIN}
    return _SLOT_RE.sub(lambda m: fill.get(m.group(1), m.group(0)), line.text)


def _renderings_for_number_check(line: Line, bank: LineBank, persona: Persona) -> list[str]:
    """Every wording the line can take, with number slots replaced by a plain word (they hold digits at run time)."""
    whats = [o.text for o in bank.what_options] if "{what}" in line.text else [""]
    labels = [f.label for f in persona.facts.values()] if "{fact_label}" in line.text else [""]
    out = []
    for w in whats:
        for lab in labels:
            text = line.text.replace("{what}", w).replace("{fact_label}", lab)
            # lower case on purpose: an UPPER-CASE stand-in after the word IFSC would be read as IFSC letters
            out.append(text.replace("{readback}", "slot").replace("{fact_value}", "slot"))
    return out


def lint(bank: LineBank, persona: Persona | None = None) -> list[str]:
    """Every rule from the plan. Returns a list of problems; empty means all lines pass."""
    persona = persona or load_persona(PERSONA_PATH)
    problems: list[str] = []

    if not MIN_LINES <= len(bank.lines) <= MAX_LINES:
        problems.append(f"there are {len(bank.lines)} lines; the plan is {MIN_LINES} to {MAX_LINES}")
    for tactic in bank.tactics:
        n = len(bank.tactic_lines(tactic))
        if not MIN_VARIANTS <= n <= MAX_VARIANTS:
            problems.append(f"{tactic}: {n} variants; each tactic needs {MIN_VARIANTS} to {MAX_VARIANTS}")
    alarm = bank.tactic_lines("alarm")
    for scenario in ("digital_arrest", "bank_kyc"):
        if sum(ln.scenario == scenario for ln in alarm) < 2:
            problems.append(f"alarm: needs at least 2 variants for {scenario}")
    if len({ln.text for ln in bank.lines}) != len(bank.lines):
        problems.append("two lines have identical text")
    for tactic, trigger in bank.triggers.items():
        if trigger not in TRIGGERS:
            problems.append(f"{tactic}: unknown trigger {trigger!r}")
    for tactic, trigger in REQUIRED_TRIGGER.items():
        if bank.triggers.get(tactic) != trigger:
            problems.append(f"{tactic}: must have trigger {trigger!r} (it only makes sense after the caller did that)")
    beta_lines = sum(bool(re.search(r"\bbeta\b", ln.text, re.IGNORECASE)) for ln in bank.lines)
    if beta_lines > MAX_BETA_FRACTION * len(bank.lines):
        problems.append(f"{beta_lines} of {len(bank.lines)} lines say 'beta'; at most a third, or Uncle sounds scripted")
    if sum(bool(_TAG_RE.findall(ln.text)) for ln in bank.lines) > MAX_TAGGED_LINES:
        problems.append(f"more than {MAX_TAGGED_LINES} lines carry a tag (tags are billed as characters)")

    for ln in bank.lines:
        where = ln.id
        text = ln.text
        if ln.scenario not in SCENARIOS:
            problems.append(f"{where}: unknown scenario {ln.scenario!r}")
        for s in ln.states:
            if s not in STATES:
                problems.append(f"{where}: unknown state {s!r}")
        if not text.isascii():
            problems.append(f"{where}: must be plain Latin script (ASCII only)")
        if any(ch.isdigit() for ch in text):
            problems.append(f"{where}: contains a numeral; digits may only arrive through slots")
        if LINK_OR_HANDLE.search(text):
            problems.append(f"{where}: contains '@', a link or a web address")

        tags = _TAG_RE.findall(text)   # read from the text itself, so an edit cannot hide a tag
        if len(tags) > 1:
            problems.append(f"{where}: more than one tag in a line")
        for tag in tags:
            if tag not in ALLOWED_TAGS:
                problems.append(f"{where}: tag {tag} is not one of {sorted(ALLOWED_TAGS)}")

        slots = set(_SLOT_RE.findall(text))
        allowed, required = SLOT_RULES.get(ln.tactic, (set(), set()))
        for slot in slots - SLOT_NAMES:
            problems.append(f"{where}: unknown slot {{{slot}}}")
        for slot in (slots & SLOT_NAMES) - allowed:
            problems.append(f"{where}: slot {{{slot}}} is not allowed in {ln.tactic}")
        for slot in required - slots:
            problems.append(f"{where}: {ln.tactic} lines must contain {{{slot}}}")
        if "{" in _SLOT_RE.sub("", text) or "}" in _SLOT_RE.sub("", text):
            problems.append(f"{where}: stray brace")

        length = len(measured_text(ln, bank, persona))
        if not MIN_CHARS <= length <= MAX_CHARS:
            problems.append(f"{where}: {length} characters once slots are filled; the range is {MIN_CHARS} to {MAX_CHARS}")

        # No wording of this line may contain anything extract_intel reads as a number, letters run or UPI id.
        for rendering in _renderings_for_number_check(ln, bank, persona):
            found = extract_intel([Transcript(rendering, 0.0)])
            if found:
                problems.append(f"{where}: extract_intel finds {[(f.kind, f.value) for f in found]} in {rendering!r}")

        if INFORMAL_FORMS.search(text):
            problems.append(f"{where}: informal form ({INFORMAL_FORMS.search(text).group(0)!r}); Uncle uses polite forms")
        if IDENTITY_WORDS.search(text):
            problems.append(f"{where}: mentions being a person, human, robot or AI; that answer belongs to the safety gate")
        if ASKS_FOR_CREDENTIALS.search(text) or ASKS_FOR_MONEY.search(text):
            problems.append(f"{where}: seems to ask the caller for money or credentials")
        if CLAIMS_AN_ACTION.search(text):
            problems.append(f"{where}: seems to say Uncle already sent, installed, clicked or shared something")
    return problems


def text_problems(text: str, max_chars: int = 100) -> list[str]:
    """The content rules from lint(), for ONE piece of free text (used on anything a language model writes).
    No slots are allowed here: the text must be final."""
    problems: list[str] = []
    if not text.strip():
        problems.append("empty")
    if len(text) > max_chars:
        problems.append(f"{len(text)} characters; the limit is {max_chars}")
    if not text.isascii():
        problems.append("not plain Latin script")
    if any(ch.isdigit() for ch in text) or LINK_OR_HANDLE.search(text) or "{" in text or "}" in text:
        problems.append("contains a numeral, '@', a link or a brace")
    tags = _TAG_RE.findall(text)
    if len(tags) > 1 or any(t not in ALLOWED_TAGS for t in tags):
        problems.append("bad tags")
    if INFORMAL_FORMS.search(text):
        problems.append("informal form")
    if IDENTITY_WORDS.search(text):
        problems.append("mentions being a person, human, robot or AI")
    if ASKS_FOR_CREDENTIALS.search(text) or ASKS_FOR_MONEY.search(text):
        problems.append("seems to ask for money or credentials")
    if CLAIMS_AN_ACTION.search(text):
        problems.append("seems to say Uncle already sent, installed, clicked or shared something")
    if text.strip() and extract_intel([Transcript(text, 0.0)]):
        problems.append("extract_intel finds a number, letters or UPI id in it")
    return problems


def review_table(bank: LineBank, persona: Persona | None = None) -> str:
    """A markdown table for reading and editing: id, state, tactic, scenario, line, length, tag."""
    persona = persona or load_persona(PERSONA_PATH)
    rows = ["| id | state | tactic | scenario | line | len | tag |", "|---|---|---|---|---|---|---|"]
    for ln in bank.lines:
        rows.append(f"| {ln.id} | {'/'.join(ln.states)} | {ln.tactic} | {ln.scenario} | {ln.text} | "
                    f"{len(measured_text(ln, bank, persona))} | {', '.join(ln.tags) or '-'} |")
    return "\n".join(rows)


def main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    bank = load_lines()
    print(review_table(bank))
    print(f"\n{len(bank.lines)} lines, {len(bank.tactics)} tactics, "
          f"{sum(bool(ln.tags) for ln in bank.lines)} with a tag")
    problems = lint(bank)
    print("\nPROBLEMS:" if problems else "\nNo problems found.")
    for p in problems:
        print(" -", p)


if __name__ == "__main__":
    main()
