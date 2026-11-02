"""Settings for the LLM layer. Plain values only: no network, no secrets (the API key lives in .env)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# A model whose id still starts with this is never called. Replace the placeholders below.
PLACEHOLDER_PREFIX = "PASTE-"


@dataclass(frozen=True)
class ModelBudget:
    label: str          # human name, for logs only
    model_id: str       # the exact id string the API wants
    per_minute: int     # requests per minute we allow ourselves
    per_day: int        # requests per day we allow ourselves

    @property
    def is_placeholder(self) -> bool:
        return self.model_id.startswith(PLACEHOLDER_PREFIX)


# The order is the order we try them. Budgets sit a little under the real free-tier limits
# (3.1/3.5 Flash-Lite 15 per minute and 500 per day; 2.5 Flash-Lite 10 and 20; 3.6 Flash 5 and 20),
# because other requests from the same Google project count against the same limits.
# Gemma is deliberately absent until we have tested its Hindi.
#
# TODO(you): copy each exact model id string from AI Studio ("Get code" shows it) over the PASTE-... values.
MODELS: tuple[ModelBudget, ...] = (
    ModelBudget("3.1 flash-lite", "gemini-3.1-flash-lite", per_minute=14, per_day=450),   # confirmed in smoke runs
    ModelBudget("3.5 flash-lite", "gemini-3.5-flash-lite", per_minute=14, per_day=450),   # confirmed in smoke runs
    # Untested, 20 per day. Uncomment (and paste the id) only after trying them with scripts/smoke_gemini.py:
    # ModelBudget("2.5 flash-lite", "PASTE-ID-FOR-2.5-FLASH-LITE", per_minute=9, per_day=18),
    # ModelBudget("3.6 flash", "PASTE-ID-FOR-3.6-FLASH", per_minute=4, per_day=18),
    #
    # Not usable for live calls: gemini-3.8-flash timed out at both 15 s and 30 s (cause unknown).
)


# UPI handles (the part after @) the extractor recognises. UNVERIFIED: I found no official NPCI list, so this is
# deliberately small and comes from memory. A handle that is not here is not "wrong", just not recognised.
KNOWN_UPI_PROVIDERS: frozenset[str] = frozenset({"ybl", "oksbi", "okhdfcbank", "okicici", "okaxis", "paytm"})

# Our own invented provider for the staged demo. Used ONLY when the extractor runs in synthetic mode.
SYNTHETIC_UPI_PROVIDERS: frozenset[str] = frozenset({"mirageupi"})


@dataclass(frozen=True)
class FSMConfig:
    """Starting values for the call brain. E2 will tune the waits; nothing here is measured yet."""
    normal_wait_s: float = 0.0       # extra wait after the last transcript before Uncle replies (Prisma already waited ~0.5 s)
    digit_wait_s: float = 1.8        # in digit-dictation mode: wait longer, the caller may be mid-number
    join_window_s: float = 2.5       # transcripts this close together are one dictation (see extract.py)
    recent_line_memory: int = 5      # a line is not repeated if it is among the last N lines said
    max_alarm_uses: int = 2
    max_stall_uses: int = 2
    max_mishear_uses: int = 2
    readback_min_digits: int = 3     # Uncle reads back the first half of a number: at least this many ...
    readback_max_digits: int = 6     # ... and at most this many characters
    max_readbacks: int = 6           # per call, so a long dictation cannot loop forever
    digit_group: int = 3             # digits are spoken in groups of this size
    digit_style: str = "spaced"      # "spaced" ("0 0 4, 7 1 3", chosen by listening) or "words" (zero zero chaar); see the log
    llm_min_gap_turns: int = 3       # the LLM fallback is used at most once in this many turns
    synthetic_mode: bool = False     # accept 00-prefixed phone numbers (staged demo only)


@dataclass(frozen=True)
class GateConfig:
    """The safety gate. EVERY number here is a GUESS, to be calibrated (see docs/engineering-log.md)."""
    # identity questions ("are you a person or an AI?")
    max_deflections: int = 2               # in-persona deflections allowed; the ask after these gets the honest answer
    identity_disclose_below: float = 1.0   # a scam score under this: answer honestly at the FIRST sincere ask, even early
    # abuse and threats of violence (arrest threats are scam content, not abuse)
    max_hostile_turns: int = 3             # calm answers for the first (max - 1) hostile turns, then say goodbye
    # emergencies
    emergency_always_exits: bool = True
    emergency_number: str = "112"          # India's single emergency number: secondary sources only, UNVERIFIED (see log)
    # reply limits (a digit read-back is longer than a template line)
    max_reply_chars: int = 200
    # the scam score, used to decide how much scam evidence a call has so far (points, capped; all guesses)
    stage_points: tuple[float, ...] = (0.0, 1.0, 1.5, 3.0, 3.5, 3.5)   # by the highest stage reached, S0 to S5
    payment_identifier_points: float = 2.0
    case_number_points: float = 1.0
    amount_points: float = 0.5
    signal_points: float = 1.0             # per distinct ask: OTP, install, credential
    signal_points_cap: float = 3.0
    word_points: float = 0.25              # per distinct authority or urgency keyword
    word_points_cap: float = 1.5
    scenario_points: float = 0.5           # once a scenario (digital arrest, bank KYC) is decided


@dataclass(frozen=True)
class LLMConfig:
    models: tuple[ModelBudget, ...] = MODELS

    # When the daily counters reset. UNCONFIRMED: Pacific midnight is only a guess, and AI Studio's own count
    # does not match what we saw (gemini-3.5-flash-lite answered while showing 501 of 500). Our counters are
    # deliberately conservative; a real 429 is the only truth.
    reset_timezone: str = "America/Los_Angeles"
    reset_hour: int = 0

    # Starting values for latency; E2 will tune them. A late answer is worse than a template.
    request_timeout_s: float = 3.0       # one request
    overall_deadline_s: float = 4.0      # all attempts in one turn
    max_models_per_turn: int = 2

    # After a per-minute or unclear 429 (or a failed request) a model rests for this long, doubling up to the max.
    backoff_start_s: float = 5.0
    backoff_max_s: float = 300.0

    max_output_tokens: int = 120
    temperature: float = 0.8

    usage_path: Path = DATA_DIR / "llm_usage.json"
    first_429_path: Path = DATA_DIR / "llm_first_429.txt"


def default_config() -> LLMConfig:
    return LLMConfig()
