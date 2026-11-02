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
class VoiceConfig:
    """The voice path: browser mic to Prisma. Prisma values are the ones we measured; the rest are GUESSES."""
    # where the server listens. Loopback only: the page can spend credits, so nothing else may reach it.
    host: str = "127.0.0.1"
    port: int = 8000

    # Prisma (headers confirmed in the smoke runs; see docs/engineering-log.md)
    prisma_url: str = "wss://api.vachana.ai/stt/v3/stream"
    prisma_model: str = "prisma-v3.0"
    language_code: str = "hi-IN"           # one locale, as decided
    transcript_format: str = "codemix"
    sample_rate: int = 8000
    frame_bytes: int = 1024                # exactly 512 samples of 16-bit audio
    min_silence_ms: int = 500              # baseline; 300 and a 6 s cap were rejected (they split numbers)
    vad_threshold: float = 0.7
    min_speech_ms: int = 200
    idle_timeout_s: int = 300              # docs: default 60, range 5 to 900
    use_keyterms: bool = False             # no evidence yet that they help (one sample each); see the log
    connect_timeout_s: float = 15.0
    max_reconnects: int = 1                # docs say nothing about reconnecting; one try, no audio replay (a guess)
    reconnect_delay_s: float = 1.0

    # pacing: a late browser must never turn into a burst of audio
    backlog_limit_s: float = 0.5           # older audio beyond this is dropped (guess)
    resync_after_s: float = 0.1            # if a frame is later than this, restart the clock instead of catching up in a burst (guess)
    browser_chunk_ms: int = 20             # how often the page posts audio (the worklet's batch size)
    voiced_rms: float = 0.005              # a frame louder than this counts as speech for the credit estimate (guess)

    # call limits
    max_call_s: float = 14 * 60.0          # Prisma cuts a stream at 15 minutes; we end before that
    tick_s: float = 1.0                    # how often the page gets a status update and limits are checked

    # credits (rates measured from a handful of runs; see the log)
    pool_credits: float = 5000.0
    hard_stop_fraction: float = 0.8
    warn_fraction: float = 0.7
    per_call_cap_credits: float = 40.0     # guess: catches a stuck loop
    stt_credits_per_min: float = 0.45
    tts_credits_per_1000_chars: float = 2.7
    seed_credits_spent: float = 18.37      # the dashboard figure at the time of writing
    seed_requests: int = 23
    ledger_path: Path = DATA_DIR / "credits.json"
    flush_every_s: float = 15.0            # the ledger is written this often during a call

    # Timbre (text to speech). Voice, language and rate were chosen by listening; the limits are GUESSES.
    timbre_url: str = "https://api.vachana.ai/api/v1/tts/sse"
    timbre_model: str = "timbre-v3.0"
    tts_voice: str = "Prakash"
    tts_language: str = "hi-en"            # Latin-script Hinglish
    tts_sample_rate: int = 24000           # 24 kHz mono 16-bit for the browser; a phone-quality 8 kHz is a later decision
    tts_speed: float = 1.0
    tts_attempt_timeout_s: float = 6.0     # one request (a short line takes about a second)
    tts_deadline_s: float = 10.0           # all attempts for one line; a late line is nearly worthless
    tts_retry_waits_s: tuple[float, ...] = (0.5, 1.0)    # waits before retry 1 and retry 2, after a 429 or a 5xx
    tts_max_chars: int = 400               # one line, tags included; a stuck loop cannot spend more per line
    tts_cache_max_lines: int = 64
    tts_cache_max_bytes: int = 20_000_000
    tts_first_429_path: Path = DATA_DIR / "timbre_first_429.txt"
    tts_log_path: Path = DATA_DIR / "tts_log.jsonl"     # one line per spoken line: timing, characters, credits (for E2 later)
    speak_test_seed: int = 0               # the fixed test line is the brain's first reply to "Hello?" with this seed
    speak_test_caller_line: str = "Hello?"

    # evidence and data. Raw audio is OFF unless MIRAGE_SAVE_AUDIO=1 is set.
    calls_dir: Path = DATA_DIR / "calls"
    audio_dir: Path = DATA_DIR / "audio"
    audio_keep_days: int = 7
    lock_path: Path = DATA_DIR / "server.lock"


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
