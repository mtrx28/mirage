"""The connection to Prisma v3.0 (speech to text) over a WebSocket.

What the docs say (docs.gnani.ai/api/STT/stt-websocket.md, re-read for this build):
  - every setting is a header on the WebSocket upgrade request, and most cannot change mid-connection;
  - audio is raw 16-bit mono PCM, exactly 1,024 bytes per frame, sent at real-time pace (no bursts);
  - the server sends `connected`, `speech_start`, `processing` (end of speech heard), `transcript` and `error`;
  - a stream lasts 15 minutes at most (then an `error` message and close code 1001);
  - `keyterms` takes up to 200 terms; non-ASCII must be JSON-escaped; an invalid list is SILENTLY ignored.
The docs say nothing about reconnecting, so a lost connection gets ONE retry here and no audio is replayed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from mirage.config import ROOT, VoiceConfig
from mirage.llm import scrub

log = logging.getLogger("mirage.prisma")

KEYTERMS_PATH = ROOT / "content" / "keyterms.json"
MAX_KEYTERMS = 200                      # the docs' limit


class PrismaError(RuntimeError):
    """A problem a person can read: it never contains the API key."""


def load_keyterms(path: Path = KEYTERMS_PATH) -> list[str]:
    """The keyterm list, checked: ASCII only, no commas or quotes, no duplicates, at most 200."""
    terms = json.loads(path.read_text(encoding="utf-8"))["terms"]
    problems = []
    if len(terms) > MAX_KEYTERMS:
        problems.append(f"{len(terms)} terms, the docs allow {MAX_KEYTERMS}")
    seen: set[str] = set()
    for term in terms:
        if not term.isascii() or any(c in term for c in ',"\\[]'):
            problems.append(f"{term!r} is not plain ASCII without commas, quotes or brackets")
        if term.lower() in seen:
            problems.append(f"{term!r} is listed twice")
        seen.add(term.lower())
    if problems:
        raise ValueError("content/keyterms.json: " + "; ".join(problems))
    return terms


def build_headers(cfg: VoiceConfig, key: str, keyterms: list[str] | None = None) -> dict[str, str]:
    """The upgrade headers. Names are the ones used (and answered) in the smoke runs, except `keyterms`,
    which no run has used yet."""
    headers = {
        "x-api-key-id": key,
        "model": cfg.prisma_model,
        "language_code": cfg.language_code,
        "format": cfg.transcript_format,
        "x-sample-rate": str(cfg.sample_rate),
        "x-vad-threshold": str(cfg.vad_threshold),
        "x-min-speech-ms": str(cfg.min_speech_ms),
        "x-min-silence-ms": str(cfg.min_silence_ms),
        "x-idle-timeout-sec": str(cfg.idle_timeout_s),
    }
    if keyterms:
        headers["keyterms"] = json.dumps(keyterms, ensure_ascii=True)       # a JSON array, as the docs allow
    return headers


OnMessage = Callable[[dict], Awaitable[None]]
OnClosed = Callable[[str], Awaitable[None]]


@dataclass
class PrismaLink:
    """One Prisma stream with a receive loop. `send_frame` is safe to call while disconnected (the frame is
    counted as lost). A drop that is not our own close gets `max_reconnects` retries, with no replay."""
    cfg: VoiceConfig
    key: str
    on_message: OnMessage
    on_closed: OnClosed
    keyterms: list[str] | None = None
    connect: Callable = websockets.connect
    connected: bool = False
    frames_lost: int = 0
    reconnects: int = 0
    _ws: object = field(default=None, repr=False)
    _reader: asyncio.Task | None = field(default=None, repr=False)
    _closing: bool = field(default=False, repr=False)
    request_ids: list[str] = field(default_factory=list)
    sent_header_names: list[str] = field(default_factory=list)      # names only, never values (the key is one)
    keyterms_header_chars: int = 0

    def _safe(self, text: object) -> str:
        return scrub(str(text), [self.key])

    async def _open_once(self) -> dict:
        headers = build_headers(self.cfg, self.key, self.keyterms)
        self.sent_header_names = sorted(headers)
        self.keyterms_header_chars = len(headers.get("keyterms", ""))
        try:
            ws = await self.connect(self.cfg.prisma_url, additional_headers=headers, ping_interval=None,
                                    open_timeout=self.cfg.connect_timeout_s)
        except InvalidStatus as e:
            code = e.response.status_code
            hint = " (the key was refused: check GNANI_API_KEY in .env)" if code == 401 else ""
            raise PrismaError(f"Prisma refused the connection with HTTP {code}{hint}.") from None
        except (OSError, asyncio.TimeoutError, WebSocketException) as e:
            raise PrismaError(f"Could not reach Prisma: {self._safe(e)}") from None
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), self.cfg.connect_timeout_s))
        except (asyncio.TimeoutError, ConnectionClosed, ValueError) as e:
            await ws.close()
            raise PrismaError(f"Prisma did not say 'connected': {self._safe(e)}") from None
        if first.get("type") != "connected":
            await ws.close()
            raise PrismaError(f"Expected 'connected' first, got {self._safe(first)[:200]}")
        self._ws, self.connected = ws, True
        if first.get("request_id"):
            self.request_ids.append(first["request_id"])
        return first

    async def open(self) -> dict:
        first = await self._open_once()
        await self.on_message(first)
        self._reader = asyncio.create_task(self._read_loop())
        return first

    async def _read_loop(self) -> None:
        reason = "closed"
        while True:
            try:
                async for raw in self._ws:
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        log.warning("Prisma sent something that is not JSON; ignored")
                        continue
                    if isinstance(message, dict):
                        await self.on_message(message)
                # A clean close ends the loop without an exception, so read the close code from the socket.
                reason = f"closed ({getattr(self._ws, 'close_code', None)})"
            except ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd else None
                reason = f"closed ({code})"
            except Exception as e:                      # a bug in a callback must not leave the call half-alive
                log.exception("receive loop failed")
                reason = f"receive loop failed: {self._safe(e)}"
            self.connected = False
            if self._closing:
                return
            # 1001 is Prisma's 15-minute limit; an idle timeout also ends the stream. Neither is worth retrying.
            if "(1001)" in reason or "(1008)" in reason or self.reconnects >= self.cfg.max_reconnects:
                await self.on_closed(reason)
                return
            self.reconnects += 1
            await asyncio.sleep(self.cfg.reconnect_delay_s)
            try:
                first = await self._open_once()
                await self.on_message({**first, "reconnected": True})
            except PrismaError as e:
                await self.on_closed(f"{reason}; reconnect failed: {e}")
                return

    async def send_frame(self, frame: bytes) -> None:
        if not self.connected:
            self.frames_lost += 1
            return
        try:
            await self._ws.send(frame)
        except ConnectionClosed:
            self.connected = False
            self.frames_lost += 1

    async def close(self) -> None:
        self._closing = True
        self.connected = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:                            # closing a dead socket is not worth reporting
                pass
        if self._reader is not None and self._reader is not asyncio.current_task():
            try:
                await asyncio.wait_for(self._reader, 3)
            except Exception:                            # includes a timeout: stop waiting for the reader
                self._reader.cancel()
