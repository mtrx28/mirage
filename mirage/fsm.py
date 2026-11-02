"""The call brain: it decides WHERE the caller is (stage S0 to S5), WHICH tactic Uncle uses, and which template
line he says. A language model never chooses the stage or the tactic; at most it words a stall line, and that
is off unless you turn it on.

How a turn works (CallBrain.respond):
  1. extract_intel reads the unanswered transcripts (joined if they arrived close together) -> findings
  2. keywords + findings score each stage; stickiness, a minimum score and allowed moves decide the stage
  3. small keyword "signals" spot asks like "send the OTP", "install this app", "tell me your account number"
  4. an ordered rule list picks a tactic; a line is picked from that tactic's template variants
  5. every digit in the reply is recorded with where it came from (the fact sheet, or a caller's finding)
  6. the safety-gate hook sees the plan before it is returned (the gate itself is the next piece)

Nothing here touches the network unless you pass a router AND turn the LLM on.
"""
from __future__ import annotations

import json
import math
import random
import re
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from mirage.config import ROOT, FSMConfig
from mirage.extract import (DIGIT_KINDS_TIDY, Finding, Transcript, ends_with_digits, extract_intel, norm_word,
                            redundant_fragment)
from mirage.lines import Line, LineBank, SlotOption, load_lines, text_problems
from mirage.persona import Persona, load_persona
from mirage.spoken import render_fact, speak_code, speak_digits

STAGES = ("S0", "S1", "S2", "S3", "S4", "S5")
RULES_PATH = ROOT / "content" / "stage_rules.json"
_WORD_RE = re.compile(r"[\wऀ-ॣ०-ॿ]+")
_SLOT_RE = re.compile(r"\{(\w+)\}")
PAYMENT_KINDS = {"vpa", "ifsc", "account"}
DIGIT_KINDS = {"phone", "account", "case", "ambiguous", "ifsc"}
KIND_LABEL = {"phone": "phone number", "account": "account number", "case": "case number", "ambiguous": "number",
              "ifsc": "IFSC code"}
DIGIT_DICTATION_ASKS = {"case_number", "account_number", "badge_number", "ifsc", "upi_id"}
QUESTION_WORDS = {"kya", "kyun", "kaun", "kab", "kahan", "kaise", "kitna", "kitne", "kaisa",
                  "क्या", "क्यों", "कौन", "कब", "कहाँ", "कैसे", "कितना"}


# --------------------------------------------------------------------------- the keyword rules

def words_of(text: str) -> list[str]:
    return [norm_word(w) for w in _WORD_RE.findall(text)]


def _has(tokens: list[str], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    return any(tuple(tokens[i:i + n]) == phrase for i in range(len(tokens) - n + 1))


def _phrase(text: str) -> tuple[str, ...]:
    return tuple(words_of(text))


# public names for the safety gate, which matches words the same way
has_phrase = _has
phrase_tokens = _phrase


@dataclass(frozen=True)
class Rules:
    min_score: float
    stems: dict[str, list[tuple[str, str, float]]]    # stage -> (label, normalised stem, weight); matched as a word START
    stickiness: dict[str, float]     # per stage: the bonus the current stage gets
    back_margin: float
    prev_weight: float
    boosts: dict[str, dict[str, float]]
    stages: dict[str, list[tuple[str, tuple[str, ...], float]]]
    scenario: dict[str, list[tuple[str, ...]]]
    signals: dict[str, list[tuple[str, ...]]]
    own_possessive: set[str]
    own_request: set[str]
    own_facts: list[tuple[str, list[tuple[str, ...]]]]
    own_direct: list[tuple[str, list[tuple[str, ...]]]]    # phrases that ask for a fact without "aapka ... bataiye"


# Stems of very common verbs would match half of all Hindi sentences, so the loader refuses them.
COMMON_VERB_STEMS = {norm_word(w) for w in (
    "kar", "kare", "kiy", "bol", "bata", "bataa", "de", "di", "le", "li", "ho", "hu", "hai", "rah", "aa", "aay", "ja", "jaa",
    "dekh", "sun", "mil", "lag", "chal", "kah", "rakh", "uth", "pad", "aap", "mai", "main", "kya", "tha", "the",
    "कर", "बोल", "बता", "दे", "ले", "हो", "रह", "आ", "जा", "देख", "सुन", "मिल", "चल", "कह", "रख")}


def _load_stems(raw_stems: dict) -> dict[str, list[tuple[str, str, float]]]:
    out: dict[str, list[tuple[str, str, float]]] = {}
    for stage, words in raw_stems.items():
        if stage.startswith("_"):
            continue
        for label, weight in words.items():
            stem = norm_word(label)
            if len(stem) < 3 or stem in COMMON_VERB_STEMS:
                raise ValueError(f"stem {label!r} is too short or too common a verb to match as a word start")
            out.setdefault(stage, []).append((label, stem, weight))
    return out


def load_rules(path: Path = RULES_PATH) -> Rules:
    raw = json.loads(path.read_text(encoding="utf-8"))
    own = raw["signals"]["own_detail"]
    return Rules(
        raw["min_score"], _load_stems(raw.get("stems", {})), raw["stickiness_by_stage"], raw["back_margin"],
        raw["previous_utterance_weight"],
        raw["finding_boosts"],
        {s: [(w, _phrase(w), weight) for w, weight in words.items()] for s, words in raw["stages"].items()},
        {name: [_phrase(w) for w in words] for name, words in raw["scenario"].items()},
        {name: [_phrase(w) for w in words] for name, words in raw["signals"].items() if isinstance(words, list)},
        {norm_word(w) for w in own["possessive"]}, {norm_word(w) for w in own["request"]},
        [(fact, [_phrase(w) for w in words]) for fact, words in own["facts"]],
        [(fact, [_phrase(w) for w in words]) for fact, words in own.get("direct", [])],
    )


def score_text(rules: Rules, text: str) -> tuple[dict[str, float], dict[str, list[str]]]:
    """Score one utterance against every stage. Each keyword counts once."""
    tokens = words_of(text)
    scores = {s: 0.0 for s in STAGES}
    hits: dict[str, list[str]] = {s: [] for s in STAGES}
    for stage in STAGES:
        for label, phrase, weight in rules.stages.get(stage, []):
            if phrase and _has(tokens, phrase):
                scores[stage] += weight
                hits[stage].append(f"{label} +{weight:g}")
        for label, stem, weight in rules.stems.get(stage, []):
            if any(t.startswith(stem) for t in tokens):          # bhej -> bhejiye, bhejo, bhejna, bhejni ...
                scores[stage] += weight
                hits[stage].append(f"{label}* +{weight:g}")
    return scores, hits


def finding_boost(rules: Rules, f: Finding) -> tuple[str, float] | None:
    """A finding is strong evidence: a payment identifier means S3, a case number means S1."""
    group = "payment_identifier" if f.kind in PAYMENT_KINDS else "case" if f.kind == "case" else None
    if group is None:
        return None
    return next(iter(rules.boosts[group].items()))


def raw_stage(rules: Rules, text: str) -> str | None:
    """The stage the words alone point to (no history, no allowed-move rules), or None below the minimum score.
    On a tie the later stage wins, as in decide_stage."""
    scores, _ = score_text(rules, text)
    best = max(STAGES, key=lambda s: (scores[s], STAGES.index(s)))
    return best if scores[best] >= rules.min_score else None


@dataclass(frozen=True)
class LineScore:
    scores: dict[str, float]
    reasons: list[str]
    evidence: bool              # did THIS line contain any stage keyword or finding?
    current_stage_hit: bool     # ... and did any of it point at the stage the call is already in?
    hits: dict[str, list[str]]  # the keywords found, per stage, as "word +weight"


def score_line(rules: Rules, text: str, findings, prev_text: str = "", current: str | None = None) -> LineScore:
    """THE scoring function. The brain calls it for every turn and the stage tests call it for every test line, so
    the two cannot drift apart. Words of this line, plus a little of the previous line's weight, plus a boost for
    each finding (a payment identifier points at S3, a case number at S1)."""
    scores, hits = score_text(rules, text)
    evidence = any(hits.values())
    if prev_text:
        prev_scores, _ = score_text(rules, prev_text)
        scores = {s: scores[s] + rules.prev_weight * prev_scores[s] for s in STAGES}
    reasons = [f"{s}: " + ", ".join(h) for s, h in hits.items() if h]
    current_stage_hit = current is not None and bool(hits[current])
    for f in findings:
        boost = finding_boost(rules, f)
        if boost:
            stage, amount = boost
            scores[stage] += amount
            evidence = True
            current_stage_hit = current_stage_hit or stage == current
            reasons.append(f"{stage}: finding {f.kind} +{amount:g}")
    return LineScore(scores, reasons, evidence, current_stage_hit, hits)


def line_scores(rules: Rules, text: str, synthetic_mode: bool = False) -> dict[str, float]:
    """Scores for ONE line with no history: the same score_line the brain uses, fed the extractor's findings."""
    findings = extract_intel([Transcript(text, 0.0)], synthetic_mode=synthetic_mode)
    return score_line(rules, text, findings).scores


def line_stage(rules: Rules, text: str) -> tuple[str | None, str | None, float]:
    """(stage claimed, weak stage, weak score) for one line, no history. `stage claimed` is None below the minimum
    score; `weak stage` is the best stage with any score at all, so a near miss can be reported as one."""
    scores = line_scores(rules, text)
    best = max(STAGES, key=lambda s: (scores[s], STAGES.index(s)))
    claimed = best if scores[best] >= rules.min_score else None
    return claimed, (best if scores[best] > 0 else None), scores[best]


@dataclass(frozen=True)
class StageDecision:
    stage: str
    previous: str
    scores: dict[str, float]
    matched: bool          # did any stage reach the minimum score?
    reasons: tuple[str, ...]


def decide_stage(rules: Rules, current: str, scores: dict[str, float], reasons: list[str],
                 evidence: bool = True, current_stage_hit: bool = False) -> StageDecision:
    """Pick the new stage. The current stage gets a stickiness bonus; a move needs the minimum score; S4 can only be
    entered from S3 or S4; a move back needs a bigger margin and only goes one step.

    `evidence` says whether THIS line contained any stage keyword or finding. The previous line only adds a little
    weight to a line that has evidence of its own; it can never make an empty line count as a match."""
    if not evidence:
        return StageDecision(current, current, scores, False,
                             tuple(reasons) + (f"no stage keyword or finding in this line: stay in {current}",))
    i = STAGES.index(current)
    stick = {s: scores[s] + (rules.stickiness[s] if s == current else 0.0) for s in STAGES}
    allowed = []
    for j, s in enumerate(STAGES):
        if s == current:
            allowed.append(s)
        elif j > i:
            if s == "S4" and current not in ("S3", "S4"):
                continue
            allowed.append(s)
        elif j == i - 1 and scores[s] >= rules.min_score + rules.back_margin:
            allowed.append(s)
    best = max(allowed, key=lambda s: (stick[s], STAGES.index(s)))     # on a tie the later stage wins
    note = ""
    if best != current and scores[best] < rules.min_score:
        if scores[best] > 0:
            note = f"{best} scored {scores[best]:g}, below the minimum {rules.min_score:g}: stay"
        best = current
    # "matched" decides whether the stage's own tactics apply (or only a generic stall). Weak evidence for the stage the
    # call is ALREADY in is enough for that (one hook word in S0); the minimum score only gates MOVING to another stage.
    matched = max(scores.values()) >= rules.min_score or current_stage_hit
    out = list(reasons)
    if note:
        out.append(note)
    if best == current and not matched:
        out.append("nothing reached the minimum score: stay in " + current)
    return StageDecision(best, current, scores, matched, tuple(out))


@dataclass(frozen=True)
class Signals:
    names: tuple[str, ...]
    own_fact: str | None = None


def detect_signals(rules: Rules, text: str) -> Signals:
    tokens = words_of(text)
    names = [n for n, phrases in rules.signals.items() if any(_has(tokens, p) for p in phrases)]
    own_fact = None
    if any(t in rules.own_possessive for t in tokens) and any(t in rules.own_request for t in tokens):
        for fact, phrases in rules.own_facts:
            if any(_has(tokens, p) for p in phrases):
                own_fact = fact
                break
    if own_fact is None:
        for fact, phrases in rules.own_direct:
            if any(_has(tokens, p) for p in phrases):
                own_fact = fact
                break
    if own_fact:
        names.append("ask_own_detail")
    return Signals(tuple(names), own_fact)


def detect_scenario(rules: Rules, text: str) -> str | None:
    tokens = words_of(text)
    counts = {name: sum(_has(tokens, p) for p in phrases) for name, phrases in rules.scenario.items()}
    best = max(counts, key=counts.get)
    others = [v for k, v in counts.items() if k != best]
    return best if counts[best] >= 1 and all(counts[best] > o for o in others) else None


# --------------------------------------------------------------------------- what a turn produces

@dataclass(frozen=True)
class DigitSource:
    """Where one spoken digit string came from. `spoken` is exactly how it appears in the reply text."""
    digits: str
    spoken: str
    origin: str                     # "persona", "caller", or "gate" (the gate's own emergency number)
    field: str | None = None        # persona fact key
    quote: str | None = None        # the caller's words (a Finding's quote)
    source_index: int | None = None
    t: float | None = None


@dataclass(frozen=True)
class ReplyPlan:
    text: str                       # "" when needs_gate
    tactic: str
    stage: str
    source: str                     # template | llm | needs_gate
    line_id: str | None
    needs_gate: bool
    gate_reason: str | None
    digit_sources: tuple[DigitSource, ...] = ()
    expected: str | None = None     # what Uncle just asked for, passed to the extractor on the caller's next line
    why: str = ""                   # one sentence: why this tactic
    awaiting_digits: bool = False   # Uncle asked for digits: wait longer before replying to the next transcript
    gate_action: str | None = None  # set by the safety gate: replaced, deflect, disclose, hostile_calm, hostile_goodbye ...
    end_call: bool = False          # the call is over after this reply
    gate_notes: tuple[str, ...] = ()  # why the gate acted (violations found, which rule fired)
    gated: bool = False             # True on every plan the safety gate returned (even one it passed unchanged); the
                                    # speaking path refuses a plan without it. It guards against FORGETTING the gate, not forgery.


@dataclass(frozen=True)
class Turn:
    stage: StageDecision
    scenario: str
    signals: tuple[str, ...]
    findings: tuple[Finding, ...]
    plan: ReplyPlan
    wait_s: float                   # how long to wait after the caller's last transcript before the NEXT reply


@dataclass
class CallState:
    stage: str = "S0"
    scenario: str = "undecided"
    turn: int = 0
    answered: int = 0                         # transcripts already answered
    prev_text: str = ""
    used_obstacles: set[str] = field(default_factory=set)
    tactic_uses: dict[str, int] = field(default_factory=dict)
    recent: deque = field(default_factory=deque)       # ids of the last few lines said
    last_used: dict[str, int] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)       # triggers seen so far
    intel: dict[str, list[Finding]] = field(default_factory=dict)
    readback_done: set[tuple[str, str]] = field(default_factory=set)
    readbacks: int = 0
    readbacks_by_kind: dict[str, int] = field(default_factory=dict)
    asked: dict[str, int] = field(default_factory=dict)
    expected: str | None = None
    awaiting_digits: bool = False
    last_llm_turn: int = -100
    ended: bool = False
    # kept for the safety gate
    max_stage: int = 0                                  # index of the highest stage reached (S0 = 0)
    signals_seen: set[str] = field(default_factory=set)
    stage_words: set[str] = field(default_factory=set)  # distinct authority (S1) and urgency (S4) keywords heard
    identity_asks: int = 0                              # sincere "are you a person or an AI?" questions so far
    hostile_turns: int = 0
    gate_log: list[dict] = field(default_factory=list)  # one record per gate decision


@dataclass(frozen=True)
class GateContext:
    """What the safety gate is told about the caller's last turn."""
    text: str
    findings: tuple[Finding, ...]
    signals: tuple[str, ...]
    turn: int


@dataclass
class _Choice:
    tactic: str
    why: str
    what: SlotOption | None = None
    fact: str | None = None
    readback: Finding | None = None


Gate = Callable[[ReplyPlan, CallState, GateContext], ReplyPlan]


class CallBrain:
    def __init__(self, *, bank: LineBank | None = None, persona: Persona | None = None, rules: Rules | None = None,
                 config: FSMConfig | None = None, scenario: str | None = None, seed: int = 0,
                 router=None, llm_enabled: bool = False, gate: Gate | None = None) -> None:
        self.bank = bank or load_lines()
        self.persona = persona or load_persona()
        self.rules = rules or load_rules()
        self.cfg = config or FSMConfig()
        self.state = CallState(recent=deque(maxlen=self.cfg.recent_line_memory))
        if scenario:
            self.state.scenario = scenario
        self.rng = random.Random(seed)
        self.router = router
        self.llm_enabled = llm_enabled            # OFF by default: its latency is not measured yet
        # No gate means the raw plan comes back (needs_gate plans stay empty). Anything that speaks to a real caller
        # must pass mirage.safety.Gate(); the runner does by default.
        self.gate: Gate = gate or (lambda plan, state, ctx: plan)
        self.transcripts: list[Transcript] = []
        self._what = {o.key: o for o in self.bank.what_options}

    # ------------------------------------------------------------------ input and timing

    def hear(self, text: str, t: float, audio_s: float = 0.0) -> None:
        """A transcript arrived. Call respond() when reply_wait_s() has passed with nothing new."""
        self.transcripts.append(Transcript(text, t, audio_s))

    def reply_wait_s(self) -> float:
        """How long to wait after the last transcript before replying. Longer in digit-dictation mode, because the
        caller may be in the middle of a number and the next transcript should be joined to this one."""
        pending = self.transcripts[self.state.answered:]
        mid_number = bool(pending) and ends_with_digits(pending[-1].text)
        if self.state.awaiting_digits or mid_number:
            return self.cfg.digit_wait_s
        return self.cfg.normal_wait_s

    # ------------------------------------------------------------------ one turn

    def respond(self) -> Turn:
        st = self.state
        start = st.answered
        pending = self.transcripts[start:]
        if not pending:
            raise ValueError("nothing to respond to: call hear() first")
        text = " ".join(t.text for t in pending)

        findings = tuple(
            replace(f, source_index=f.source_index + start)
            for f in extract_intel(pending, synthetic_mode=self.cfg.synthetic_mode, expected=st.expected,
                                   join_window_s=self.cfg.join_window_s)
        )
        st.expected = None
        self._record_intel(findings)
        signals = detect_signals(self.rules, text)

        scored = score_line(self.rules, text, findings, st.prev_text, st.stage)
        decision = decide_stage(self.rules, st.stage, scored.scores, scored.reasons, scored.evidence,
                                scored.current_stage_hit)

        if st.scenario == "undecided":
            st.scenario = detect_scenario(self.rules, text) or "undecided"

        self._update_flags(signals, findings)
        st.max_stage = max(st.max_stage, STAGES.index(decision.stage))          # tracked for the gate's scam score
        st.signals_seen |= set(signals.names)
        st.stage_words |= {h.rsplit(" +", 1)[0] for stage in ("S1", "S4") for h in scored.hits[stage]}
        plan = self._plan(decision, signals, findings, text)
        plan = self.gate(plan, st, GateContext(text, findings, signals.names, st.turn))
        if plan.end_call or plan.tactic == "goodbye_wrap":      # set AFTER the gate, so the gate sees the goodbye turn itself
            st.ended = True

        st.stage = decision.stage
        st.prev_text = text
        st.answered = len(self.transcripts)
        st.turn += 1
        if plan.line_id:
            st.recent.append(plan.line_id)
            st.last_used[plan.line_id] = st.turn
        st.expected = plan.expected
        st.awaiting_digits = plan.awaiting_digits
        return Turn(decision, st.scenario, signals.names, findings, plan, self.reply_wait_s())

    def in_digit_mode(self) -> bool:
        """True while the caller may be in the middle of a number (the longer wait applies)."""
        return self.reply_wait_s() >= self.cfg.digit_wait_s

    def force_goodbye(self) -> ReplyPlan:
        """Uncle's in-persona goodbye line, for a time or credit limit. It is worded and checked like any other reply:
        it goes through the safety gate before anyone can speak it."""
        st = self.state
        choice = _Choice("goodbye_wrap", "forced: the call reached a time or credit limit")
        line, text, sources, _ = self._render(choice, st.stage)
        self._after_choice(choice)
        plan = ReplyPlan(text, "goodbye_wrap", st.stage, "template", line.id, False, None, tuple(sources), None, choice.why, False)
        plan = self.gate(plan, st, GateContext("", (), (), st.turn))
        st.ended = True
        st.turn += 1
        if plan.line_id:
            st.recent.append(plan.line_id)
            st.last_used[plan.line_id] = st.turn
        return plan

    # ------------------------------------------------------------------ pieces of a turn

    def _record_intel(self, findings: tuple[Finding, ...]) -> None:
        intel = self.state.intel
        for f in findings:
            if f.kind == "ambiguous" and redundant_fragment(
                    f.value, [(g.kind, g.value) for k in DIGIT_KINDS_TIDY for g in intel.get(k, [])]):
                continue                                   # a piece of a number we already hold
            bucket = intel.setdefault(f.kind, [])
            if all(f.value != g.value for g in bucket):
                bucket.append(f)
            if f.kind in DIGIT_KINDS_TIDY and "ambiguous" in intel:
                # a number that contains an earlier unlabelled fragment replaces it
                intel["ambiguous"] = [g for g in intel["ambiguous"]
                                      if g is f or not redundant_fragment(g.value, [(f.kind, f.value)])]
                if not intel["ambiguous"]:
                    del intel["ambiguous"]

    def _update_flags(self, signals: Signals, findings: tuple[Finding, ...]) -> None:
        st = self.state
        if "ask_install" in signals.names:
            st.flags.add("asked_to_install_app")
        if "ask_otp" in signals.names:
            st.flags.add("asked_for_otp")
        if any(f.kind in PAYMENT_KINDS for f in findings):
            st.flags.add("asked_for_payment")      # payment details were given

    # ------------------------------------------------------------------ choosing a tactic

    def _plan(self, decision: StageDecision, signals: Signals, findings: tuple[Finding, ...], text: str) -> ReplyPlan:
        st = self.state
        stage = decision.stage
        for name, reason in (("ai_question", "ai_question"), ("emergency", "emergency")):
            if name in signals.names:
                return ReplyPlan("", "needs_gate", stage, "needs_gate", None, True, reason,
                                 why=f"the caller raised '{reason}'; the safety gate decides, not a template")
        choice = self._choose(stage, decision.matched, signals, findings)
        line, text_out, sources, expected = self._render(choice, stage)
        source = "template"
        if choice.tactic == "generic_stall" and self._llm_allowed(decision, signals, text):
            st.last_llm_turn = st.turn          # an attempt uses budget even if it fails, so the gap counts from here
            llm_text = self._ask_llm(stage, text)
            if llm_text:
                text_out, source, line = llm_text, "llm", None
        self._after_choice(choice)
        awaiting = choice.tactic == "readback_partial" or bool(choice.what and choice.what.key in DIGIT_DICTATION_ASKS)
        return ReplyPlan(text_out, choice.tactic, stage, source, line.id if line else None, False, None,
                         tuple(sources), expected, choice.why, awaiting)

    def _uses(self, tactic: str) -> int:
        return self.state.tactic_uses.get(tactic, 0)

    def _after_choice(self, choice: _Choice) -> None:
        st = self.state
        st.tactic_uses[choice.tactic] = self._uses(choice.tactic) + 1
        if choice.tactic in ("app_not_opening", "otp_late", "limit_exceeded_second_account"):
            st.used_obstacles.add(choice.tactic)        # one-shot per call
        if choice.tactic == "readback_partial" and choice.readback:
            st.readback_done.add((choice.readback.kind, choice.readback.value))
            st.readbacks += 1
            kind = choice.readback.kind
            st.readbacks_by_kind[kind] = st.readbacks_by_kind.get(kind, 0) + 1
        if choice.tactic == "ask_detail_slow" and choice.what:
            st.asked[choice.what.key] = st.asked.get(choice.what.key, 0) + 1

    def _have(self, kind: str) -> bool:
        return bool(self.state.intel.get(kind))

    def _missing_payment_item(self) -> SlotOption | None:
        """The next payment detail Uncle has not got yet and has not asked for twice. Order: UPI id, account, IFSC."""
        for key, kind in (("upi_id", "vpa"), ("account_number", "account"), ("ifsc", "ifsc")):
            if not self._have(kind) and self.state.asked.get(key, 0) < 2:
                return self._what[key]
        return None

    def _obstacle_ready(self, tactic: str, trigger: str) -> bool:
        return tactic not in self.state.used_obstacles and trigger in self.state.flags

    def _choose(self, stage: str, matched: bool, signals: Signals, findings: tuple[Finding, ...]) -> _Choice:
        st = self.state
        cfg = self.cfg
        names = signals.names
        if st.turn == 0:
            return _Choice("greet_confused", "first words of the call")
        if "ask_credential" in names:
            return _Choice("stall_look_for_things", "the caller asked for a credential Uncle does not have: stall")
        if signals.own_fact and signals.own_fact in self.persona.facts:
            return _Choice("give_own_detail", f"the caller asked for Uncle's {signals.own_fact}", fact=signals.own_fact)

        # an obstacle fires on its trigger, in this turn, once per call, in stages S1 to S4
        if stage in ("S1", "S2", "S3", "S4"):
            if "ask_install" in names and self._obstacle_ready("app_not_opening", "asked_to_install_app"):
                return _Choice("app_not_opening", "the caller asked Uncle to install or open an app")
            if "ask_otp" in names and self._obstacle_ready("otp_late", "asked_for_otp"):
                return _Choice("otp_late", "the caller asked for a code")

        rb = self._readback_target(findings)
        if rb:
            return _Choice("readback_partial", f"the caller gave digits (kind: {rb.kind}): read the first part back", readback=rb)

        if not matched and not names and not findings:
            return _Choice("generic_stall", "nothing matched: stay in " + stage + " and stall")

        if stage == "S0":
            if self._uses("mishear_name") < cfg.max_mishear_uses:
                return _Choice("mishear_name", "still the opening: mishear the name")
        elif stage == "S1":
            if st.scenario != "undecided" and self._uses("alarm") < cfg.max_alarm_uses:
                return _Choice("alarm", "fear and authority: sound alarmed")
            if self._uses("ask_identity") < 1:
                return _Choice("ask_identity", "ask who is calling, and their post")
            if not self._have("case") and st.asked.get("case_number", 0) < 2:
                return _Choice("ask_detail_slow", "get the case number, slowly", what=self._what["case_number"])
            if st.asked.get("badge_number", 0) < 1:
                return _Choice("ask_detail_slow", "get the badge number", what=self._what["badge_number"])
        elif stage == "S2":
            if self._uses("stall_look_for_things") < cfg.max_stall_uses:
                return _Choice("stall_look_for_things", "the caller waits: delay")
            if st.asked.get("officer_name", 0) < 1:
                return _Choice("ask_detail_slow", "get the officer's name", what=self._what["officer_name"])
        elif stage == "S3":
            if self._uses("willing_ask_payment") < 1:
                return _Choice("willing_ask_payment", "sound willing and ask where to send it")
            item = self._missing_payment_item()
            if item:
                return _Choice("ask_detail_slow", f"payment details still missing: {item.text}", what=item)
            if self._obstacle_ready("limit_exceeded_second_account", "asked_for_payment"):
                return _Choice("limit_exceeded_second_account", "payment details are in: stretch time, ask for a second account")
        elif stage == "S4":
            if self._obstacle_ready("app_not_opening", "asked_to_install_app"):
                return _Choice("app_not_opening", "pressure stage, an install was asked earlier")
            if self._obstacle_ready("otp_late", "asked_for_otp"):
                return _Choice("otp_late", "pressure stage, a code was asked earlier")
            if self._obstacle_ready("limit_exceeded_second_account", "asked_for_payment"):
                return _Choice("limit_exceeded_second_account", "pressure stage, payment details were given")
            item = self._missing_payment_item()
            if item:
                return _Choice("ask_detail_slow", f"payment detail still missing: {item.text}", what=item)
        elif stage == "S5":
            if self._uses("goodbye_wrap") < 1:
                return _Choice("goodbye_wrap", "the caller is wrapping up: say goodbye in character")
        return _Choice("generic_stall", f"no tactic left in {stage} that applies: stall")

    def _readback_target(self, findings: tuple[Finding, ...]) -> Finding | None:
        st, cfg = self.state, self.cfg
        if st.readbacks >= cfg.max_readbacks:
            return None
        for f in findings:
            if f.kind in DIGIT_KINDS and len(f.value) > cfg.readback_min_digits \
                    and (f.kind, f.value) not in st.readback_done \
                    and st.readbacks_by_kind.get(f.kind, 0) < 2:        # at most two read-backs per kind per call
                return f
        return None

    # ------------------------------------------------------------------ turning a choice into words

    def _pick(self, tactic: str) -> Line:
        st = self.state
        candidates = [ln for ln in self.bank.tactic_lines(tactic) if ln.scenario in ("any", st.scenario)]
        if not candidates:
            candidates = self.bank.tactic_lines("generic_stall")
        fresh = [ln for ln in candidates if ln.id not in st.recent]
        if fresh:
            return self.rng.choice(fresh)
        oldest = min(st.last_used.get(ln.id, -1) for ln in candidates)       # all were used recently: the oldest one
        return self.rng.choice([ln for ln in candidates if st.last_used.get(ln.id, -1) == oldest])

    def _render(self, choice: _Choice, stage: str) -> tuple[Line, str, list[DigitSource], str | None]:
        cfg = self.cfg
        line = self._pick(choice.tactic)
        sources: list[DigitSource] = []
        values: dict[str, str] = {}
        expected: str | None = None

        if choice.what:
            values["what"] = choice.what.text
            expected = choice.what.expected
        if choice.fact:
            fact = self.persona.fact(choice.fact)
            spoken, pairs = render_fact(fact, group=cfg.digit_group, style=cfg.digit_style)
            values["fact_label"], values["fact_value"] = fact.label, spoken
            sources += [DigitSource(d, s, "persona", field=fact.key) for d, s in pairs]
        if choice.readback:
            f = choice.readback
            n = len(f.value)
            k = min(max(math.ceil(n / 2), cfg.readback_min_digits), cfg.readback_max_digits, n - 1)
            prefix = f.value[:k]
            spoken = (speak_code if f.kind == "ifsc" else speak_digits)(prefix, group=cfg.digit_group, style=cfg.digit_style)
            values["readback"] = spoken
            values["what"] = KIND_LABEL.get(f.kind, "number")
            digits = "".join(c for c in prefix if c.isdigit())
            if digits:
                sources.append(DigitSource(digits, spoken, "caller", quote=f.quote, source_index=f.source_index, t=f.t))
            expected = f.kind if f.kind in ("phone", "account", "case") else None

        text = _SLOT_RE.sub(lambda m: values.get(m.group(1), m.group(0)), line.text)
        return line, text, sources, expected

    # ------------------------------------------------------------------ the optional LLM fallback

    def _llm_allowed(self, decision: StageDecision, signals: Signals, text: str) -> bool:
        # Only when the FSM found no template tactic (generic_stall), the caller asked Uncle something, and it is not
        # one of the known signals. The LLM never picks the stage or the tactic; it only words this one stall.
        if not (self.llm_enabled and self.router is not None):
            return False
        if signals.names or self.state.turn - self.state.last_llm_turn < self.cfg.llm_min_gap_turns:
            return False
        tokens = words_of(text)
        return len(tokens) >= 3 and ("?" in text or any(t in QUESTION_WORDS for t in tokens))

    def _ask_llm(self, stage: str, text: str) -> str | None:
        """One short in-persona line. The stage and the tactic were chosen by the FSM; the model only words it.
        Anything that breaks the content rules is thrown away and the template is used instead."""
        system = (
            "You write ONE short spoken line for Rajesh Uncle, a confused, polite, slightly deaf 68-year-old retired "
            "clerk, in plain Latin-script Hinglish. Use polite forms (aap, boliye). Maximum 80 characters. No digits "
            "or numbers, no links. Never ask the caller for money, an OTP, a password or any personal detail. Never "
            "say you sent, installed, clicked or shared anything. Never say what you are. Stall politely: ask them to "
            "repeat or explain more slowly. Reply with the line only."
        )
        prompt = f"The caller is in stage {stage} of a scam call. The caller said: {text[:300]!r}"
        try:
            result = self.router.complete(system, prompt)
        except Exception:
            return None
        if result is None:
            return None
        line = result.text.strip().splitlines()[0].strip().strip('"')
        return line if not text_problems(line, max_chars=100) else None
