"""One call: browser microphone in, Prisma transcripts out, credits and limits watched on the way.

Stop 1 of the voice path: there is no brain and no Timbre yet, so Uncle is silent and the page shows what Prisma
heard. The safety gate is already REQUIRED here, so no later stop can speak without one.

How a call runs:
  1. the page says "start" with the microphone's sample rate; we check the credit ledger, open Prisma, and say "ready";
  2. the page streams int16 microphone chunks; we resample to 8 kHz, cut exact frames, and a Pacer sends one every 64 ms;
  3. Prisma's messages are logged; each transcript goes to the page, to the evidence log and to the credit meter;
  4. once a second a ticker updates the page and checks the limits (14 minutes, the credit hard stop, the per-call cap);
  5. the call ends on hang-up, a limit, or a Prisma problem; the credits go into the ledger and a summary is written.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from mirage.audio import Framer, Pacer, Resampler
from mirage.calllog import CallLog
from mirage.config import VoiceConfig
from mirage.credits import CallMeter, CreditLimitReached, Ledger
from mirage.gaterequired import GateRequired, require_gate  # noqa: F401  (re-exported: tests and the server import them from here)
from mirage.llm import scrub
from mirage.prisma import PrismaError, PrismaLink

log = logging.getLogger("mirage.session")

Notify = Callable[[dict], Awaitable[None]]


class CallSession:
    def __init__(self, cfg: VoiceConfig, gate: object, ledger: Ledger, notify: Notify, *, key: str,
                 keyterms: list[str] | None = None, link_factory: Callable[..., PrismaLink] = PrismaLink,
                 call_log: CallLog | None = None, max_call_s: float | None = None) -> None:
        self.gate = require_gate(gate)
        self.cfg, self.ledger, self.notify, self._key = cfg, ledger, notify, key
        self.keyterms = keyterms
        self._link_factory = link_factory
        self.max_call_s = cfg.max_call_s if max_call_s is None else max_call_s
        self.log = call_log or CallLog(cfg)
        self.meter = CallMeter(cfg, ledger)
        self.pacer: Pacer | None = None
        self.link: PrismaLink | None = None
        self.resampler: Resampler | None = None
        self.framer = Framer(cfg.frame_bytes)
        self.end_reason: str | None = None
        self.done = asyncio.Event()
        self.transcripts: list[dict] = []
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self._ending = False
        self._t_start = time.monotonic()

    # ------------------------------------------------------------------ start
    def _safe(self, text: object) -> str:
        return scrub(str(text), [self._key])

    async def start(self, browser_rate: int) -> None:
        if self._started:
            raise RuntimeError("this call has already started")
        self._started = True
        if self.ledger.blocked():
            self.log.event("refused", why="credit hard stop")
            await self._finish("credit_limit_at_start")
            raise CreditLimitReached(
                f"Credit hard stop: the ledger shows {self.ledger.spent:.2f} credits, at or over the allowed "
                f"{self.ledger.limit:.0f}. Check the dashboard and run 'python -m mirage.credits sync <figure>'.")
        try:
            self.resampler = Resampler(browser_rate)
        except ValueError as e:
            await self._finish("bad_audio_format")
            raise PrismaError(str(e)) from None
        self.log.event("start", browser_rate=browser_rate, keyterms=len(self.keyterms or []),
                       model=self.cfg.prisma_model, language=self.cfg.language_code, format=self.cfg.transcript_format)
        self.link = self._link_factory(cfg=self.cfg, key=self._key, on_message=self._on_prisma,
                                       on_closed=self._on_prisma_closed, keyterms=self.keyterms)
        try:
            await self.link.open()
        except PrismaError as e:
            self.log.event("prisma_open_failed", error=str(e))
            await self._finish("prisma_unavailable")
            raise
        self.log.event("prisma_headers_sent", names=self.link.sent_header_names,
                       keyterms_header_chars=self.link.keyterms_header_chars)
        self.pacer = Pacer(self._send_frame, self.cfg)
        self._tasks = [asyncio.create_task(self.pacer.run()), asyncio.create_task(self._ticker())]
        self.meter.flush(force=True)                         # counts this call as one request in the ledger
        await self.notify({"type": "ready", "max_call_s": self.max_call_s})

    async def _send_frame(self, frame: bytes) -> None:
        self.log.audio(frame)
        await self.link.send_frame(frame)

    # ------------------------------------------------------------------ audio in
    def feed(self, pcm: bytes) -> None:
        """Microphone audio from the page: int16 mono at the browser's sample rate."""
        if self.pacer is None or self._ending:
            return
        for frame in self.framer.push(self.resampler.process(pcm)):
            self.pacer.push(frame)

    # ------------------------------------------------------------------ Prisma messages
    async def _on_prisma(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "connected":
            self.log.event("prisma_connected", request_id=msg.get("request_id"), config=msg.get("config"),
                           reconnected=bool(msg.get("reconnected")))
            if msg.get("reconnected"):
                await self.notify({"type": "notice", "message": "The speech service reconnected."})
        elif kind in ("speech_start", "processing"):
            self.log.event(kind, utterance_id=msg.get("utterance_id"))
        elif kind == "transcript":
            text = (msg.get("text") or "").strip()
            audio_s = (msg.get("audio_duration_ms") or 0) / 1000.0
            self.meter.reported_s += audio_s
            record = {"text": text, "audio_s": round(audio_s, 3), "server_latency_ms": msg.get("latency"),
                      "language": msg.get("detected_language_code"), "segment_id": msg.get("segment_id"),
                      "utterance_id": msg.get("utterance_id")}
            self.log.transcript(**record, raw=msg)                   # the whole message as Prisma sent it
            if text:
                self.transcripts.append({"t": round(self.log.now(), 2), **record})
                await self.notify({"type": "transcript", "text": text, "t": round(self.log.now(), 1)})
        elif kind == "error":
            message = self._safe(msg.get("message"))
            self.log.event("prisma_error", message=message)
            await self.notify({"type": "notice", "message": f"Speech service: {message}"})
        else:
            self.log.event("prisma_other", message_type=kind)

    async def _on_prisma_closed(self, reason: str) -> None:
        self.log.event("prisma_closed", reason=reason)
        await self._finish("prisma_closed")

    # ------------------------------------------------------------------ limits and status
    async def _ticker(self) -> None:
        while not self._ending:
            await asyncio.sleep(self.cfg.tick_s)
            self._sync_meter()
            self.meter.flush()
            held = self.log.now()
            extra = self.meter.credits - self.meter._flushed
            await self.notify({
                "type": "tick", "held_s": round(held, 1), "call_credits": round(self.meter.credits, 3),
                "ledger_spent": round(self.ledger.spent + extra, 2), "pool": self.cfg.pool_credits,
                "pool_fraction": round(self.ledger.fraction_of_pool(extra), 4),
                "warning": self.ledger.warning(extra),
            })
            if held >= self.max_call_s:
                await self._finish("time_limit")
            elif self.meter.over_pool_limit():
                await self._finish("credit_limit")
            elif self.meter.over_call_cap():
                await self._finish("call_credit_cap")

    def _sync_meter(self) -> None:
        if self.pacer is not None:
            frame_s = self.pacer.frame_s
            self.meter.voiced_s = self.pacer.voiced_frames * frame_s
            self.meter.sent_s = self.pacer.real_frames * frame_s

    # ------------------------------------------------------------------ end
    async def hangup(self, reason: str = "hangup") -> None:
        await self._finish(reason)

    async def _finish(self, reason: str) -> None:
        if self._ending:
            return
        self._ending = True
        self.end_reason = reason
        if self.pacer is not None:
            self.pacer.stop()
        me = asyncio.current_task()
        for task in self._tasks:
            if task is not me:
                task.cancel()
        if self.link is not None:
            await self.link.close()
        self._sync_meter()
        if self.pacer is not None:                           # a call that never streamed audio costs nothing
            self.meter.flush(force=True)
        summary = {"end_reason": reason, "credits": self.meter.summary(), "transcripts": len(self.transcripts)}
        if self.pacer is not None:
            p = self.pacer
            summary["audio"] = {"real_frames": p.real_frames, "filler_frames": p.filler_frames,
                                "muted_frames": p.muted_frames, "dropped_frames": p.dropped_frames,
                                "resyncs": p.resyncs, "voiced_frames": p.voiced_frames}
        if self.link is not None:
            summary["prisma"] = {"request_ids": self.link.request_ids, "reconnects": self.link.reconnects,
                                 "frames_lost": self.link.frames_lost}
        self.log.event("end", reason=reason)
        self.log.finish(summary)
        try:
            await self.notify({"type": "ended", "reason": reason, "call_credits": round(self.meter.credits, 3),
                               "ledger_spent": round(self.ledger.spent, 2), "pool": self.cfg.pool_credits,
                               "pool_fraction": round(self.ledger.fraction_of_pool(), 4),
                               "warning": self.ledger.warning(), "held_s": round(self.log.now(), 1),
                               "transcripts": len(self.transcripts)})
        except Exception:                                    # the page may already be gone
            pass
        self.done.set()
