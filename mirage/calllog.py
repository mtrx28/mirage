"""Per-call evidence on disk: data/calls/<call id>/. Plain files, one JSON object per line.

    events.jsonl       everything that happened, with seconds since the call started
    transcripts.jsonl  each final transcript from Prisma
    summary.json       written at the end: reason, length, credits, counters
    (stop 3 adds findings, the stage timeline, gate decisions and latency)

Raw audio is NOT saved unless MIRAGE_SAVE_AUDIO=1 is set in the environment. If it is, it goes to
data/audio/<call id>/ and `purge_old_audio` deletes anything older than the configured number of days
(the server runs it at startup). data/ is gitignored.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

from mirage.config import VoiceConfig


def new_call_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + os.urandom(2).hex()


def audio_saving_enabled() -> bool:
    return os.environ.get("MIRAGE_SAVE_AUDIO", "").strip() == "1"


class CallLog:
    def __init__(self, cfg: VoiceConfig | None = None, call_id: str | None = None, *,
                 clock=time.monotonic) -> None:
        self.cfg = cfg or VoiceConfig()
        self.call_id = call_id or new_call_id()
        self.dir = self.cfg.calls_dir / self.call_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self.t0 = clock()
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._audio: wave.Wave_write | None = None
        if audio_saving_enabled():
            folder = self.cfg.audio_dir / self.call_id
            folder.mkdir(parents=True, exist_ok=True)
            self._audio = wave.open(str(folder / "mic_8k.wav"), "wb")
            self._audio.setnchannels(1)
            self._audio.setsampwidth(2)
            self._audio.setframerate(self.cfg.sample_rate)

    def now(self) -> float:
        """Seconds since the call started."""
        return self._clock() - self.t0

    def _append(self, name: str, record: dict) -> None:
        with (self.dir / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def event(self, kind: str, **fields) -> None:
        self._append("events.jsonl", {"t": round(self.now(), 3), "event": kind, **fields})

    def transcript(self, **fields) -> None:
        self._append("transcripts.jsonl", {"t": round(self.now(), 3), **fields})

    def record(self, stream: str, **fields) -> None:
        """One more line in `<stream>.jsonl` (turns, latency ...), stamped with seconds since the call started."""
        self._append(f"{stream}.jsonl", {"t": round(self.now(), 3), **fields})

    def write_json(self, name: str, body: object) -> None:
        (self.dir / name).write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")

    def audio(self, frame: bytes) -> None:
        if self._audio is not None:
            self._audio.writeframes(frame)

    def finish(self, summary: dict) -> None:
        if self._audio is not None:
            self._audio.close()
            self._audio = None
        body = {"call_id": self.call_id, "started_at": self.started_at, "length_s": round(self.now(), 1),
                "audio_saved": audio_saving_enabled(), **summary}
        (self.dir / "summary.json").write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")


def purge_old_audio(cfg: VoiceConfig | None = None, *, now: float | None = None) -> list[str]:
    """Delete saved-audio folders older than cfg.audio_keep_days. Returns the names removed."""
    cfg = cfg or VoiceConfig()
    removed: list[str] = []
    if not cfg.audio_dir.exists():
        return removed
    cutoff = (now if now is not None else time.time()) - cfg.audio_keep_days * 86400
    for folder in cfg.audio_dir.iterdir():
        if folder.is_dir() and folder.stat().st_mtime < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(folder.name)
    return removed
