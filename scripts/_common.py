"""Shared helpers for the smoke-test scripts: key loading, secret scrubbing, CSV log."""
from __future__ import annotations

import csv
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SMOKE_DIR = ROOT / "data" / "smoke"
LOG_PATH = SMOKE_DIR / "credit_log.csv"
LOG_FIELDS = [
    "timestamp", "script", "label", "model", "audio_seconds", "speech_seconds",
    "characters", "first_result_s", "wall_s", "balance_before", "balance_after", "note",
]

# Hindi text must not crash printing or file writing, even if output is redirected.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_secrets: list[str] = []


def load_gnani_key() -> str:
    """Read GNANI_API_KEY from .env. Only ever reports whether it was found."""
    load_dotenv(ROOT / ".env")
    key = os.environ.get("GNANI_API_KEY", "").strip()
    if not key:
        sys.exit("GNANI_API_KEY is missing. Copy .env.example to .env and fill it in.")
    _secrets.append(key)
    print("GNANI_API_KEY found: yes")
    return key


def scrub(value: object) -> str:
    """Turn anything into text with the API key blanked out, for safe printing."""
    text = str(value)
    for secret in _secrets:
        text = text.replace(secret, "***")
    return text


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(f"{question} [y/N] ").strip().lower() == "y"


def log_run(**row: object) -> None:
    """Append one row to data/smoke/credit_log.csv. Balance columns are left for you to fill in."""
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow({"timestamp": datetime.now().isoformat(timespec="seconds"), **row})
    print(f"Logged to {LOG_PATH.relative_to(ROOT)} (fill in balance_before / balance_after by hand)")
