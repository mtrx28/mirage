"""Credit protection: count what a call spends, keep a running total on disk, and stop before the pool runs out.

The numbers are ESTIMATES made in code from audio seconds and characters sent, using rates we measured from a
handful of runs (config.py). The dashboard is the truth; `python -m mirage.credits sync <figure>` copies the
dashboard figure into the ledger whenever the two drift apart.

    python -m mirage.credits                       show the ledger
    python -m mirage.credits sync 18.37 --requests 23
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mirage.config import VoiceConfig
from mirage.llm import atomic_write_text


class CreditLimitReached(RuntimeError):
    """The hard stop: the ledger is at or over the allowed share of the pool."""


class Ledger:
    """The running total of credits spent, in one small JSON file. One process at a time (see the README)."""

    def __init__(self, cfg: VoiceConfig | None = None, *, path: Path | None = None) -> None:
        self.cfg = cfg or VoiceConfig()
        self.path = path or self.cfg.ledger_path
        self.spent, self.requests = self._load()

    # ---- numbers
    @property
    def limit(self) -> float:
        return self.cfg.pool_credits * self.cfg.hard_stop_fraction

    @property
    def warn_at(self) -> float:
        return self.cfg.pool_credits * self.cfg.warn_fraction

    def fraction_of_pool(self, extra: float = 0.0) -> float:
        return (self.spent + extra) / self.cfg.pool_credits

    def blocked(self, extra: float = 0.0) -> bool:
        return self.spent + extra >= self.limit

    def warning(self, extra: float = 0.0) -> bool:
        return self.spent + extra >= self.warn_at

    # ---- file
    def _load(self) -> tuple[float, int]:
        """Read the ledger. A missing file starts from the seed; an unreadable one blocks everything (the real
        total is unknown, so spending is not allowed until `sync` fixes it)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return float(data["spent"]), int(data.get("requests", 0))
        except FileNotFoundError:
            self._save(self.cfg.seed_credits_spent, self.cfg.seed_requests, note="seeded from the dashboard figure")
            return self.cfg.seed_credits_spent, self.cfg.seed_requests
        except (OSError, ValueError, KeyError, TypeError):
            corrupt = self.path.with_name(self.path.name + ".corrupt")
            try:
                self.path.replace(corrupt)
            except OSError:
                pass
            # Unknown total: treat the pool as spent. `sync` writes a good file and unblocks.
            return self.cfg.pool_credits, 0

    def _save(self, spent: float, requests: int, note: str = "") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {"spent": round(spent, 4), "requests": requests, "pool": self.cfg.pool_credits,
                "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "note": note}
        atomic_write_text(self.path, json.dumps(body, indent=2))

    def add(self, credits: float, requests: int = 0, note: str = "") -> None:
        self.spent += credits
        self.requests += requests
        self._save(self.spent, self.requests, note)

    def sync(self, spent: float, requests: int | None = None) -> None:
        """Replace the total with the dashboard figure."""
        self.spent = spent
        if requests is not None:
            self.requests = requests
        self._save(self.spent, self.requests, note="synced to the dashboard by hand")


@dataclass
class CallMeter:
    """What ONE call has spent. Speech-to-text is counted two ways and the larger is used, so the estimate
    leans high (the pool is protected): seconds of audio that sounded like speech to a simple loudness check,
    and the seconds Prisma itself reports in its transcripts. Silence is not billed, so the loudness count
    ignores quiet frames. Text-to-speech is counted in characters (tags and brackets included)."""
    cfg: VoiceConfig
    ledger: Ledger
    voiced_s: float = 0.0
    reported_s: float = 0.0
    sent_s: float = 0.0          # all real-mic seconds sent (kept for the log only)
    tts_chars: int = 0
    _flushed: float = field(default=0.0, repr=False)
    _last_flush: float = field(default_factory=time.monotonic, repr=False)
    _counted_request: bool = field(default=False, repr=False)

    @property
    def stt_s(self) -> float:
        return max(self.voiced_s, self.reported_s)

    @property
    def stt_credits(self) -> float:
        return self.stt_s / 60.0 * self.cfg.stt_credits_per_min

    @property
    def tts_credits(self) -> float:
        return self.tts_chars / 1000.0 * self.cfg.tts_credits_per_1000_chars

    @property
    def credits(self) -> float:
        return self.stt_credits + self.tts_credits

    def over_call_cap(self) -> bool:
        return self.credits >= self.cfg.per_call_cap_credits

    def over_pool_limit(self) -> bool:
        return self.ledger.blocked(self.credits - self._flushed)

    def flush(self, *, force: bool = False) -> None:
        """Write what this call has spent so far into the ledger (so a crash loses at most a few seconds)."""
        now = time.monotonic()
        if not force and now - self._last_flush < self.cfg.flush_every_s:
            return
        delta = self.credits - self._flushed
        first = not self._counted_request
        if delta > 0 or first:
            self.ledger.add(delta, requests=1 if first else 0, note="call in progress" if not force else "call ended")
            self._flushed += delta
            self._counted_request = True
        self._last_flush = now

    def summary(self) -> dict:
        return {"stt_voiced_s": round(self.voiced_s, 1), "stt_reported_s": round(self.reported_s, 1),
                "stt_sent_s": round(self.sent_s, 1), "stt_credits": round(self.stt_credits, 3),
                "tts_chars": self.tts_chars, "tts_credits": round(self.tts_credits, 3),
                "call_credits": round(self.credits, 3), "ledger_after": round(self.ledger.spent, 3)}


def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Show or correct the credit ledger.")
    sub = p.add_subparsers(dest="command")
    sync = sub.add_parser("sync", help="set the ledger to the figure on your Gnani dashboard")
    sync.add_argument("spent", type=float, help="credits consumed, as the dashboard shows")
    sync.add_argument("--requests", type=int, default=None, help="request count, as the dashboard shows")
    args = p.parse_args(argv)

    ledger = Ledger()
    if args.command == "sync":
        before = ledger.spent
        ledger.sync(args.spent, args.requests)
        print(f"Ledger set to {ledger.spent:.2f} credits (was {before:.2f}; difference {ledger.spent - before:+.2f}).")
    cfg = ledger.cfg
    print(f"Spent (ledger):   {ledger.spent:.2f} credits, {ledger.requests} requests")
    print(f"Pool:             {cfg.pool_credits:.0f}; warning at {ledger.warn_at:.0f}, hard stop at {ledger.limit:.0f} "
          f"({cfg.hard_stop_fraction:.0%})")
    print(f"Used:             {ledger.fraction_of_pool():.2%} of the pool")
    print(f"File:             {ledger.path}")
    if ledger.blocked():
        print("STATE: BLOCKED. Calls will not start. Run 'sync' with the dashboard figure if the ledger is wrong.")


if __name__ == "__main__":
    main()
