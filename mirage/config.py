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
    ModelBudget("3.1 flash-lite", "PASTE-ID-FOR-3.1-FLASH-LITE", per_minute=14, per_day=450),
    ModelBudget("3.5 flash-lite", "PASTE-ID-FOR-3.5-FLASH-LITE", per_minute=14, per_day=450),
    ModelBudget("2.5 flash-lite", "PASTE-ID-FOR-2.5-FLASH-LITE", per_minute=9, per_day=18),
    ModelBudget("3.6 flash", "PASTE-ID-FOR-3.6-FLASH", per_minute=4, per_day=18),
)


@dataclass(frozen=True)
class LLMConfig:
    models: tuple[ModelBudget, ...] = MODELS

    # When the daily counters reset. UNCONFIRMED: Pacific midnight is only a belief; change it when you know.
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
