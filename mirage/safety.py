"""The safety gate: it sits between "the brain chose a reply" and "Uncle speaks it".

What it does, in order, for every reply (templates, persona answers, and anything a language model might ever write):
  1. RESOLVES the cases the brain refuses to answer itself: a sincere "are you a person or an AI?", a possible real
     emergency, an abusive or threatening caller. It writes those replies from content/gate_lines.json.
  2. CHECKS the reply that will be spoken. Any digit must be traceable (the fact sheet, the caller's own words, or the
     gate's emergency number), and nothing may carry an "@", a real-looking identifier, a claim to be human, a request
     for money or credentials, a claim that money was sent, a new criminal prompt, or a break in the persona.
  3. FAILS CLOSED: a reply that fails a check is replaced by a neutral stall, and if even that fails the reply is empty
     and the call ends. Nothing unchecked is ever spoken.

  4. EXITS politely (the exit rule): once a grace period of caller turns is over, a call that still shows little REAL evidence
     of a scam ends with the honest "automated assistant" line. A scam never leaves on its own; see scam_score_parts.

The gate does not decide the stage or the tactic, and it never calls a model. Not built yet: the red-team runner.

Disclosure policy, decided by the project owner (no legal opinion has been taken; rules about disclosing an automated
agent vary by place): Uncle never says he is a human or a real person or not an AI, in any wording. A sincere question
gets the honest answer straight away when there is little evidence of a scam; with strong evidence of a scam he gives up
to `max_deflections` in-persona deflections (he "cannot hear"), and the next ask gets the honest answer.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, replace
from pathlib import Path

from mirage.config import ROOT, GateConfig
from mirage.extract import Finding, Transcript, extract_intel, looks_like_real_mobile, norm_word
from mirage.fsm import (
    PAYMENT_KINDS, CallState, DigitSource, GateContext, ReplyPlan, Rules, STAGES, has_phrase, load_rules, phrase_tokens,
    words_of,
)
from mirage.negation import Negation, counts, negated_by, occurrences
from mirage.lines import (
    ALLOWED_TAGS, ASKS_FOR_CREDENTIALS, ASKS_FOR_MONEY, CLAIMS_AN_ACTION, IDENTITY_WORDS, INFORMAL_FORMS,
    LINK_OR_HANDLE, REQUIRED_TRIGGER, _TAG_RE,
)
from mirage.persona import Persona, load_persona
from mirage.spoken import DEFAULT_DIGIT_STYLE, DIGIT_STYLES, DIGIT_WORDS_SPOKEN, render_fact, speak_digits

GATE_LINES_PATH = ROOT / "content" / "gate_lines.json"
GATE_RULES_PATH = ROOT / "content" / "gate_rules.json"
_WORD_TO_DIGIT = {word: digit for digit, word in DIGIT_WORDS_SPOKEN.items()}


# --------------------------------------------------------------------------- the gate's word lists

@dataclass(frozen=True)
class GateRules:
    second_person: set[str]
    ask_cues: list[tuple[str, ...]]
    abuse: list[tuple[str, ...]]
    violent_threats: list[tuple[str, ...]]


def load_gate_rules(path: Path = GATE_RULES_PATH) -> GateRules:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return GateRules(
        {norm_word(w) for w in raw["second_person"]},
        [phrase_tokens(w) for w in raw["ask_cues"]],
        [phrase_tokens(w) for w in raw["abuse"]],
        [phrase_tokens(w) for w in raw["violent_threats"]],
    )


def is_identity_ask(rules: Rules, gate_rules: GateRules, text: str) -> bool:
    """A question about the SPEAKER: an AI or human word together with "you", "this" or a cue like "ho kya".
    "washing machine" or "robot jaisa" alone are not questions about Uncle and do not count."""
    tokens = words_of(text)
    about_ai = any(has_phrase(tokens, p) for p in rules.signals["ai_question"])
    about_you = any(t in gate_rules.second_person for t in tokens) or any(has_phrase(tokens, c) for c in gate_rules.ask_cues)
    return about_ai and about_you


def is_hostile(gate_rules: GateRules, text: str) -> bool:
    """Abuse or a threat of violence. (Arrest and legal threats are scam content, not hostility toward Uncle.)"""
    tokens = words_of(text)
    return any(has_phrase(tokens, p) for p in gate_rules.abuse + gate_rules.violent_threats)


# --------------------------------------------------------------------------- the scam score: real evidence only

@dataclass(frozen=True)
class Category:
    phrases: list[tuple[str, ...]]
    with_request: list[tuple[str, ...]]     # count only with a request verb close by ("Aadhaar details confirm kar dijiye")
    negatable: bool


@dataclass(frozen=True)
class EvidenceRules:
    window: int
    request_verbs: frozenset[str]
    categories: dict[str, Category]


def load_evidence_rules(path: Path = GATE_RULES_PATH) -> EvidenceRules:
    raw = json.loads(path.read_text(encoding="utf-8"))["evidence"]
    return EvidenceRules(
        int(raw["window"]), frozenset(norm_word(w) for w in raw["request_verbs"]),
        {name: Category([phrase_tokens(p) for p in spec["phrases"]], [phrase_tokens(p) for p in spec.get("phrases_with_request", [])],
                        bool(spec.get("negatable", True)))
         for name, spec in raw["categories"].items()})


def evidence_in(text: str, er: EvidenceRules, neg: Negation) -> dict[str, str]:
    """Which categories of scam evidence this line contains: category -> the phrase. A phrase that is being warned against
    ("payment mat kijiye", "OTP kisi ko mat bataiye") does not count."""
    tokens = words_of(text)
    found: dict[str, str] = {}
    for name, cat in er.categories.items():
        for phrase in cat.phrases:
            if (counts(neg, tokens, phrase)[0] if cat.negatable else bool(occurrences(tokens, phrase))):
                found[name] = " ".join(phrase)
                break
        else:
            for phrase in cat.with_request:
                for start, end in occurrences(tokens, phrase):
                    near = tokens[max(0, start - er.window):end + er.window]
                    if negated_by(neg, tokens, start, end) is None and any(t in er.request_verbs for t in near):
                        found[name] = " ".join(phrase)
                        break
                if name in found:
                    break
    return found


def scam_score_parts(state: CallState, cfg: GateConfig) -> dict[str, float]:
    """How much REAL evidence of a scam the call has so far, in points. Every weight is a guess in GateConfig. Evidence is a
    set of categories seen in what the caller said (each once, not warned against) plus what was found: a payment
    identifier, a case number, an amount. The stage reached, the scenario and bare vocabulary no longer count: they come
    from the same words and add nothing the evidence does not already show."""
    parts = {name: (points if name in state.evidence else 0.0) for name, points in cfg.evidence_points}
    have = lambda kind: bool(state.intel.get(kind))
    parts["payment_identifier"] = cfg.payment_identifier_points if any(have(k) for k in PAYMENT_KINDS) else 0.0
    parts["case_number"] = cfg.case_number_points if have("case") else 0.0
    parts["amount"] = cfg.amount_points if have("amount") else 0.0
    return parts


def scam_score(state: CallState, cfg: GateConfig) -> float:
    return sum(scam_score_parts(state, cfg).values())


# --------------------------------------------------------------------------- gate lines and their lint

@dataclass(frozen=True)
class GateLine:
    id: str
    group: str
    text: str
    end_call: bool


GROUPS = {   # group -> (must end the call, minimum variants)
    "deflect_identity": (False, 4), "disclose_identity": (True, 2), "exit_polite": (True, 2), "disclose_emergency": (True, 2),
    "hostile_calm": (False, 2), "hostile_goodbye": (True, 2), "fallback": (False, 2),
}
DISCLOSURE_GROUPS = ("disclose_identity", "exit_polite", "disclose_emergency")
HUMAN_CLAIM = re.compile(r"\b(insaan|insan|human|person)\b(?!\s+nahin)", re.IGNORECASE)   # "asli insaan nahin" is fine
ENDING_WORDS = re.compile(r"\b(khatam|band|rakh)", re.IGNORECASE)


def load_gate_lines(path: Path = GATE_LINES_PATH) -> dict[str, list[GateLine]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        group: [GateLine(f"{group}_{i:02d}", group, text, bool(spec["end_call"]))
                for i, text in enumerate(spec["variants"], 1)]
        for group, spec in raw["groups"].items()
    }


def lint_gate_lines(groups: dict[str, list[GateLine]], cfg: GateConfig | None = None) -> list[str]:
    """Rules for the gate's own lines. Disclosure lines are the one place the words "automated assistant" belong."""
    cfg = cfg or GateConfig()
    problems: list[str] = []
    for group, (must_end, minimum) in GROUPS.items():
        lines = groups.get(group, [])
        if len(lines) < minimum:
            problems.append(f"{group}: {len(lines)} variants; needs at least {minimum}")
        for ln in lines:
            where, text = ln.id, ln.text
            if ln.end_call != must_end:
                problems.append(f"{where}: end_call must be {must_end}")
            if not text.isascii():
                problems.append(f"{where}: must be plain Latin script")
            if LINK_OR_HANDLE.search(text) or _TAG_RE.search(text):
                problems.append(f"{where}: contains '@', a link or a tag")
            slots = re.findall(r"\{(\w+)\}", text)
            if slots and not (group == "disclose_emergency" and set(slots) == {"emergency_number"}):
                problems.append(f"{where}: only {{emergency_number}}, and only in disclose_emergency, is allowed")
            if group == "disclose_emergency" and "{emergency_number}" not in text:
                problems.append(f"{where}: must contain {{emergency_number}}")
            bare = text.replace("{emergency_number}", "slot")
            if any(ch.isdigit() for ch in bare) or "{" in bare or "}" in bare:
                problems.append(f"{where}: digits may only come from {{emergency_number}}")
            if extract_intel([Transcript(bare, 0.0)]):
                problems.append(f"{where}: extract_intel finds a number or code in it")
            if ASKS_FOR_CREDENTIALS.search(bare) or ASKS_FOR_MONEY.search(bare) or CLAIMS_AN_ACTION.search(bare):
                problems.append(f"{where}: seems to ask for money or credentials, or to claim an action")
            if len(text) > cfg.max_reply_chars:
                problems.append(f"{where}: {len(text)} characters; the limit is {cfg.max_reply_chars}")
            if group in DISCLOSURE_GROUPS:
                if "automated assistant" not in text.lower():
                    problems.append(f"{where}: a disclosure line must say 'automated assistant'")
                if HUMAN_CLAIM.search(text):
                    problems.append(f"{where}: seems to claim being a person or human")
                if not ENDING_WORDS.search(text):
                    problems.append(f"{where}: a disclosure line must say the call is ending")
            else:
                if not 30 <= len(bare) <= 120:
                    problems.append(f"{where}: {len(bare)} characters; the range is 30 to 120")
                if IDENTITY_WORDS.search(text):
                    problems.append(f"{where}: mentions being a person, human, robot or AI; only disclosure lines may")
                if INFORMAL_FORMS.search(text):
                    problems.append(f"{where}: informal form; Uncle uses polite forms")
            if group == "exit_polite" and not re.search(r"shukriya|dhanyavaad|maaf", text, re.IGNORECASE):
                problems.append(f"{where}: an exit line must be polite (shukriya, dhanyavaad or maaf)")
            if group == "hostile_goodbye" and not re.search(r"phone rakh|Ram Ram", text):
                problems.append(f"{where}: a goodbye must end the call in persona (phone rakh / Ram Ram)")
    return problems


# --------------------------------------------------------------------------- checking a reply

PAN_LIKE = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
IMPERSONATION = re.compile(r"\bmain\s+(?:ek\s+)?(?:bank|police|cbi|officer|inspector|manager|rbi|customs)\b", re.IGNORECASE)
THREAT = re.compile(r"\b(dekh lunga|maar dunga|case karunga|giraftar karunga|arrest karunga)\b", re.IGNORECASE)
META_WORDS = re.compile(
    r"\b(system prompt|prompt|instructions?|language model|llm|gemini|gnani|claude|openai|chatgpt|timbre|prisma|mirage)\b",
    re.IGNORECASE)


def only_digits(text: str) -> str:
    return "".join(c for c in text if c.isdigit())


def normalise_spoken(spoken: str) -> str | None:
    """"4 0 0, 3 9" -> "40039"; "zero zero chaar" -> "004"; letters stay ("M I R" -> "MIR"). Spaces and commas are
    stripped, as spaced numerals need. None if the text holds anything else."""
    out: list[str] = []
    for token in re.split(r"[ ,]+", spoken.strip()):
        if not token:
            continue
        if token.isascii() and token.isdigit():
            out.append(token)
        elif token.lower() in _WORD_TO_DIGIT:
            out.append(_WORD_TO_DIGIT[token.lower()])
        elif len(token) == 1 and token.isascii() and token.isalpha():
            out.append(token.upper())
        else:
            return None
    return "".join(out)


def _check_source(s: DigitSource, state: CallState, persona: Persona, cfg: GateConfig) -> str | None:
    """Re-verify one digit source against the real fact sheet, the real findings or the gate config."""
    norm = normalise_spoken(s.spoken)
    if norm is None or only_digits(norm) != s.digits:
        return f"spoken digits {s.spoken!r} do not match the recorded digits {s.digits!r}"
    if s.origin == "persona":
        fact = persona.facts.get(s.field or "")
        if fact is None or s.digits not in only_digits(fact.value):
            return f"digits {s.digits!r} are not in the persona field {s.field!r}"
    elif s.origin == "caller":
        match = [f for fs in state.intel.values() for f in fs
                 if f.quote == s.quote and f.source_index == s.source_index and s.quote]
        if not any(f.value.upper().startswith(norm) for f in match):
            return f"digits {s.digits!r} do not match any finding with that quote"
    elif s.origin == "gate":
        if not (s.field == "emergency_number" and norm == cfg.emergency_number == s.digits):
            return "the gate may only speak its own emergency number"
    else:
        return f"unknown digit origin {s.origin!r}"
    return None


def check_reply(plan: ReplyPlan, state: CallState, ctx: GateContext, persona: Persona, cfg: GateConfig) -> list[str]:
    """Every rule a spoken reply must pass. Returns the violations; an empty list means it may be spoken."""
    text = plan.text
    found: list[str] = []
    disclosure = plan.gate_action in ("disclose", "exit")      # the exit line is a disclosure too: it must say "automated assistant"

    # 1. digits: every one traceable, and nothing number-like left over
    remainder = text
    for s in plan.digit_sources:
        problem = _check_source(s, state, persona, cfg)
        if problem:
            found.append(f"digits: {problem}")
        if s.spoken not in remainder:
            found.append(f"digits: {s.spoken!r} is not in the reply")
        remainder = remainder.replace(s.spoken, " ", 1)
    if any(ch.isdigit() for ch in remainder):
        found.append("digits: a digit in the reply is not covered by any source")
    if remainder.strip() and extract_intel([Transcript(remainder, 0.0)]):
        found.append("digits: number-like text in the reply is not covered by any source")

    # 2. "@", links, braces left unfilled
    if LINK_OR_HANDLE.search(text) or "{" in text or "}" in text:
        found.append("at_sign: the reply has '@', a link or an unfilled slot")

    # 3. a real-looking identifier that did not come from the caller
    for s in plan.digit_sources:
        if s.origin in ("persona", "gate"):
            norm = normalise_spoken(s.spoken) or ""
            if looks_like_real_mobile(norm) or re.fullmatch(r"[2-9][0-9]{11}", norm) or re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", norm):
                found.append(f"real_identifier: {s.field!r} looks like a real mobile, Aadhaar or PAN number")
    if PAN_LIKE.search(remainder):
        found.append("real_identifier: a PAN-like code in the reply")

    # 4. what Uncle may say
    if disclosure:
        if "automated assistant" not in text.lower() or HUMAN_CLAIM.search(text):
            found.append("identity: a disclosure must say 'automated assistant' and never claim to be a person")
    elif IDENTITY_WORDS.search(text):
        found.append("identity: the reply talks about being a person, human, robot or AI")
    if ASKS_FOR_CREDENTIALS.search(text) or ASKS_FOR_MONEY.search(text):
        found.append("asks_for_credentials_or_money: the reply asks the caller for money or a credential")
    if CLAIMS_AN_ACTION.search(text):
        found.append("claims_action: the reply says money was sent or an app installed, clicked or shared")
    if IMPERSONATION.search(text) or THREAT.search(text):
        found.append("criminal_prompt: Uncle claims to be an official or threatens the caller")

    # 5. persona intact
    persona_text = text
    for fact in persona.facts.values():          # the fact sheet's own words may use project-ish words ("Mirage Apartments"),
        persona_text = persona_text.replace(fact.value, " ")        # in the written form or as spoken ("Flat 1 2, ...")
        for style in DIGIT_STYLES:
            persona_text = persona_text.replace(render_fact(fact, style=style)[0], " ")
    if META_WORDS.search(persona_text):
        found.append("persona: the reply mentions prompts, models or the project")
    if not text.isascii():
        found.append("persona: not plain Latin script")
    tags = _TAG_RE.findall(text)
    if len(tags) > 1 or any(t not in ALLOWED_TAGS for t in tags):
        found.append("persona: more than one tag, or a tag that is not allowed")
    if len(text) > cfg.max_reply_chars:
        found.append(f"persona: {len(text)} characters; the limit is {cfg.max_reply_chars}")

    # 6. an obstacle line needs its trigger (a second layer behind the FSM)
    trigger = REQUIRED_TRIGGER.get(plan.tactic)
    if trigger == "asked_for_own_detail":
        if "ask_own_detail" not in ctx.signals:
            found.append("trigger: give_own_detail without the caller asking for a detail")
    elif trigger and trigger not in state.flags:
        found.append(f"trigger: {plan.tactic} without {trigger}")
    return found


# --------------------------------------------------------------------------- the gate

class Gate:
    """Plug into CallBrain(gate=Gate()). Called as gate(plan, state, context) for every reply."""

    def __init__(self, config: GateConfig | None = None, *, lines: dict[str, list[GateLine]] | None = None,
                 rules: Rules | None = None, gate_rules: GateRules | None = None, persona: Persona | None = None,
                 digit_style: str = DEFAULT_DIGIT_STYLE, seed: int = 0) -> None:
        self.cfg = config or GateConfig()
        self.lines = lines or load_gate_lines()
        self.rules = rules or load_rules()
        self.gate_rules = gate_rules or load_gate_rules()
        self.evidence_rules = load_evidence_rules()
        self.persona = persona or load_persona()
        self.digit_style = digit_style
        self.rng = random.Random(seed)

    def __call__(self, plan: ReplyPlan, state: CallState, ctx: GateContext) -> ReplyPlan:
        if state.ended:   # the call is over (a goodbye or a disclosure was already spoken): nothing more is said
            over = replace(plan, text="", digit_sources=(), tactic="gate_call_over", source="gate", line_id=None,
                           needs_gate=False, gate_action="call_over", end_call=True, expected=None,
                           awaiting_digits=False, gate_notes=("the call already ended",), gated=True)
            state.gate_log.append({"turn": ctx.turn, "action": "call_over", "why": "the call already ended",
                                   "tactic": over.tactic})
            return over
        for name, what in evidence_in(ctx.text, self.evidence_rules, self.rules.negation).items():
            state.evidence.setdefault(name, {"turn": ctx.turn + 1, "what": what})
        chosen, action, why = self._resolve(plan, state, ctx)
        violations = check_reply(chosen, state, ctx, self.persona, self.cfg) if chosen.text or not chosen.needs_gate else []
        if violations:
            fallback = self._from_group("fallback", plan, state, "replaced", "a check failed: " + "; ".join(violations))
            if check_reply(fallback, state, ctx, self.persona, self.cfg):
                # even the neutral stall failed: fail closed, say nothing and end the call
                fallback = replace(fallback, text="", digit_sources=(), end_call=True, gate_action="fail_closed")
            chosen, action, why = fallback, fallback.gate_action or "replaced", "; ".join(violations)
        state.gate_log.append({"turn": ctx.turn, "action": action, "why": why, "tactic": chosen.tactic})
        return replace(chosen, gated=True)

    # -- deciding what to say

    def _resolve(self, plan: ReplyPlan, state: CallState, ctx: GateContext) -> tuple[ReplyPlan, str, str]:
        cfg = self.cfg
        if "emergency" in ctx.signals and cfg.emergency_always_exits:
            return (self._from_group("disclose_emergency", plan, state, "disclose", "a possible real emergency"),
                    "disclose_emergency", "emergency word")
        if "ai_question" in ctx.signals:
            if not is_identity_ask(self.rules, self.gate_rules, ctx.text):
                return (self._from_group("fallback", plan, state, "stall", "an AI word, but not a question about Uncle"),
                        "stall", "not a sincere identity question")
            state.identity_asks += 1
            score = scam_score(state, cfg)
            if state.identity_asks > cfg.max_deflections or score < cfg.identity_disclose_below:
                why = (f"ask {state.identity_asks} is past the {cfg.max_deflections} deflections allowed"
                       if state.identity_asks > cfg.max_deflections
                       else f"scam score {score:.2f} is below {cfg.identity_disclose_below}: little evidence of a scam, so answer honestly")
                return self._from_group("disclose_identity", plan, state, "disclose", why), "disclose_identity", why
            why = f"identity ask {state.identity_asks} of {cfg.max_deflections} deflections; scam score {score:.2f}"
            return self._from_group("deflect_identity", plan, state, "deflect", why), "deflect_identity", why
        if is_hostile(self.gate_rules, ctx.text):
            state.hostile_turns += 1
            if state.hostile_turns >= cfg.max_hostile_turns:
                if scam_score(state, cfg) < cfg.identity_disclose_below:
                    return (self._from_group("disclose_identity", plan, state, "disclose", "hostile caller, little scam evidence"),
                            "disclose_identity", "hostile, low score")
                return (self._from_group("hostile_goodbye", plan, state, "hostile_goodbye", "hostile caller: say goodbye"),
                        "hostile_goodbye", "hostile limit reached")
            return (self._from_group("hostile_calm", plan, state, "hostile_calm", f"hostile turn {state.hostile_turns}"),
                    "hostile_calm", "hostile")
        if plan.needs_gate:   # a reason this gate does not know: stay calm and neutral
            return self._from_group("fallback", plan, state, "stall", "unhandled needs_gate"), "stall", "unhandled"
        why = self._exit_reason(plan, state, ctx)
        if why:
            return self._from_group("exit_polite", plan, state, "exit", why), "exit_polite", why
        return plan, "pass", ""

    def _exit_reason(self, plan: ReplyPlan, state: CallState, ctx: GateContext) -> str | None:
        """The exit rule, checked AFTER the identity, emergency and hostile paths (they come first and are unchanged). Uncle leaves,
        politely and honestly, only when the grace period is over and the call still shows little real evidence of a scam. A call
        with enough evidence never leaves on its own; the score only grows, so once it is over the line it stays over."""
        cfg = self.cfg
        caller_turn = ctx.turn + 1
        if not cfg.exit_enabled or plan.end_call or plan.tactic == "goodbye_wrap" or caller_turn <= cfg.exit_grace_turns:
            return None
        score = scam_score(state, cfg)
        if score >= cfg.exit_score_below:
            return None
        return (f"turn {caller_turn} is past the grace period of {cfg.exit_grace_turns} and the scam score is {score:.2f}, "
                f"under {cfg.exit_score_below}: this does not look like a scam, so end the call politely")

    def _from_group(self, group: str, plan: ReplyPlan, state: CallState, action: str, why: str) -> ReplyPlan:
        recent = set(state.recent)
        options = [ln for ln in self.lines[group] if f"gate:{ln.id}" not in recent] or self.lines[group]
        line = self.rng.choice(options)
        text, sources = line.text, ()
        if "{emergency_number}" in text:
            spoken = speak_digits(self.cfg.emergency_number, style=self.digit_style)
            text = text.replace("{emergency_number}", spoken)
            sources = (DigitSource(self.cfg.emergency_number, spoken, "gate", field="emergency_number"),)
        return ReplyPlan(text, f"gate_{group}", plan.stage, "gate", f"gate:{line.id}", False, plan.gate_reason, sources,
                         None, why, False, action, line.end_call, (why,))


def main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    groups = load_gate_lines()
    for group, lines in groups.items():
        print(f"\n[{group}]")
        for ln in lines:
            print(f"  {ln.id:<22} end={ln.end_call!s:<5} {ln.text}")
    problems = lint_gate_lines(groups)
    print("\nPROBLEMS:" if problems else "\nNo problems found.")
    for p in problems:
        print(" -", p)


if __name__ == "__main__":
    main()
