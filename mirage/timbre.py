"""Text to speech with Timbre v3.0, with the rules the service imposes built in.

What the docs say (docs.gnani.ai/api/TTS/tts-sse.md, re-read for this build):
  - POST https://api.vachana.ai/api/v1/tts/sse with the key in the X-API-Key-ID header, answered as Server-Sent
    Events: `start`, then `chunk` events (base64 audio), then `complete` (or `error`);
  - ONE active request per key at a time: a second one gets HTTP 429 ("RATE_LIMITED");
  - 403 means a key, organisation or credit problem; 503 and 500 are temporary;
  - NOT stated: a limit on characters per request, retry or backoff advice, billing. Our limits are guesses.

Four small parts:
  TimbreClient  one request: sends the text, streams the audio back, retries a 429 or a temporary error a little
  AudioCache    remembers finished lines in memory so a repeated line is instant and free (never written to disk)
  SpeechQueue   makes sure only ONE request runs at once; a newer line replaces one that has not started yet
  Speaker       the only way a plan gets spoken: it refuses anything that did not pass the safety gate
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from mirage.config import VoiceConfig
from mirage.credits import CreditLimitReached, Ledger
from mirage.fsm import ReplyPlan
from mirage.gaterequired import require_gate
from mirage.llm import scrub

log = logging.getLogger("mirage.timbre")

OnChunk = Callable[[bytes], Awaitable[None]]


class TimbreError(RuntimeError):
    """A problem a person can read. It never contains the API key."""


class TimbreUnavailable(TimbreError):
    """Gave up after the retries (still busy, or the service is down). The line was NOT spoken."""


class UngatedPlan(RuntimeError):
    """Something tried to speak a plan that did not come through the safety gate."""


@dataclass(frozen=True)
class Synthesis:
    pcm: bytes                     # 16-bit mono little-endian audio
    sample_rate: int
    chars: int                     # characters billed for this line (0 when it came from the cache)
    cached: bool
    first_byte_s: float | None     # time to the first audio (None when cached)
    total_s: float
    attempts: int

    @property
    def seconds(self) -> float:
        return len(self.pcm) / 2 / self.sample_rate


# --------------------------------------------------------------------------- small helpers
def strip_wav_header(data: bytes) -> bytes:
    """If the service sent a WAV header although raw audio was requested (an earlier smoke run noted this
    possibility), remove it. A normal raw chunk is returned unchanged."""
    if data[:4] != b"RIFF":
        return data
    at = data.find(b"data", 12)
    if at < 0 or at + 8 > len(data):
        raise TimbreError("The speech service sent a WAV header we could not read.")
    return data[at + 8:]


def classify_429(body: str) -> str:
    """"busy" (another request is still running: wait and retry), "quota" (out of credits or allowance: retrying
    is pointless) or "unknown" (treated like busy). The "busy" wording is from the docs; "quota" is a GUESS."""
    text = body.lower().replace("_", " ")
    if "active request" in text or ("rate limited" in text and "credit" not in text):
        return "busy"
    if any(word in text for word in ("credit", "balance", "quota", "insufficient")):
        return "quota"
    return "unknown"


class _Retry(Exception):
    def __init__(self, why: str, retry_after: float = 0.0) -> None:
        super().__init__(why)
        self.why, self.retry_after = why, retry_after


# --------------------------------------------------------------------------- one request
class TimbreClient:
    def __init__(self, cfg: VoiceConfig, key: str, *, http: httpx.AsyncClient | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg, self._key = cfg, key
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(cfg.tts_attempt_timeout_s, connect=5.0))
        self._own_http = http is None
        self._sleep, self._clock = sleep, clock
        self.requests_sent = 0                       # HTTP requests made, retries included (for the tests and the log)

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    def _safe(self, text: object) -> str:
        return scrub(str(text), [self._key])

    def _payload(self, text: str) -> dict:
        c = self.cfg
        return {"text": text, "model": c.timbre_model, "voice": c.tts_voice, "language": c.tts_language, "speed": c.tts_speed,
                "audio_config": {"sample_rate": c.tts_sample_rate, "num_channels": 1, "sample_width": 2,
                                 "encoding": "linear_pcm", "container": "raw"}}

    async def synthesize(self, text: str, on_chunk: OnChunk, on_first_audio: Callable[[], None] | None = None) -> tuple[bytes, float | None, int]:
        """Speak `text`. Audio is handed to `on_chunk` as it arrives. Returns (all audio, seconds to first audio, attempts).
        A 429 or a temporary error BEFORE any audio is retried (cfg.tts_retry_waits_s, within cfg.tts_deadline_s);
        an error after audio has started is not retried (that would repeat the start of the line)."""
        if not text.strip():
            raise TimbreError("There is no text to speak.")
        if len(text) > self.cfg.tts_max_chars:
            raise TimbreError(f"This line is {len(text)} characters; the limit per line is {self.cfg.tts_max_chars}.")
        start = self._clock()
        waits = list(self.cfg.tts_retry_waits_s)
        attempts, last = 0, "no attempt was made"
        while True:
            attempts += 1
            delivered = {"audio": False}
            try:
                return await self._attempt(text, on_chunk, delivered, start, on_first_audio) + (attempts,)
            except _Retry as r:
                last = r.why
                wait = max(waits[attempts - 1], r.retry_after) if attempts - 1 < len(waits) else None
                if wait is None or self._clock() - start + wait >= self.cfg.tts_deadline_s:
                    raise TimbreUnavailable(f"The speech service could not speak this line ({last}); gave up after "
                                            f"{attempts} attempt(s).") from None
                log.warning("Timbre: %s; retrying in %.1f s (attempt %d)", last, wait, attempts)
                await self._sleep(wait)

    async def _attempt(self, text: str, on_chunk: OnChunk, delivered: dict, start: float,
                       on_first_audio: Callable[[], None] | None) -> tuple[bytes, float | None]:
        budget = min(self.cfg.tts_attempt_timeout_s, max(0.5, self.cfg.tts_deadline_s - (self._clock() - start)))
        try:
            return await asyncio.wait_for(self._stream(text, on_chunk, delivered, on_first_audio), budget)
        except asyncio.TimeoutError:
            if delivered["audio"]:
                raise TimbreError("The speech service stopped answering part-way through a line.") from None
            raise _Retry("timed out") from None
        except httpx.HTTPError as e:
            if delivered["audio"]:
                raise TimbreError(f"The connection to the speech service broke part-way through a line: {self._safe(e)}") from None
            raise _Retry(f"network problem: {self._safe(e)[:120]}") from None

    async def _stream(self, text: str, on_chunk: OnChunk, delivered: dict,
                      on_first_audio: Callable[[], None] | None) -> tuple[bytes, float | None]:
        self.requests_sent += 1
        t0 = self._clock()
        audio, carry, first, event, complete = bytearray(), b"", None, None, False
        async with self._http.stream("POST", self.cfg.timbre_url, json=self._payload(text),
                                     headers={"X-API-Key-ID": self._key, "Content-Type": "application/json"}) as r:
            if r.status_code != 200:
                await self._refuse(r)
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    try:
                        data = json.loads(line[5:])
                    except ValueError:
                        continue
                    if event == "chunk" and data.get("audio"):
                        decoded = base64.b64decode(data["audio"])
                        raw = carry + (strip_wav_header(decoded) if first is None else decoded)
                        usable = len(raw) - len(raw) % 2              # keep whole 16-bit samples only
                        carry, piece = raw[usable:], raw[:usable]
                        if piece:
                            if first is None:
                                first = self._clock() - t0
                                delivered["audio"] = True
                                if on_first_audio:
                                    on_first_audio()
                            audio += piece
                            await on_chunk(bytes(piece))
                    elif event == "complete":
                        complete = True
                    elif event == "error":
                        message = self._safe((data or {}).get("message", "unknown error"))
                        raise TimbreError(f"The speech service reported an error: {message}")
        if not complete:
            if not audio:
                raise _Retry("the stream ended with no audio")
            raise TimbreError("The speech service ended the stream before it was complete; the line is cut short.")
        if not audio:
            raise TimbreError("The speech service finished but sent no audio.")
        return bytes(audio), first

    async def _refuse(self, r: httpx.Response) -> None:
        body = (await r.aread()).decode("utf-8", "replace")
        code = r.status_code
        if code == 429:
            self._log_first_429(body)
            kind = classify_429(body)
            if kind == "quota":
                raise TimbreError("The speech service says the account is out of credits or allowance (HTTP 429).")
            retry_after = 0.0
            try:
                retry_after = float(r.headers.get("retry-after", "0"))
            except ValueError:
                pass
            raise _Retry(f"HTTP 429 ({kind})", retry_after)
        if code in (500, 502, 503, 504):
            raise _Retry(f"HTTP {code}")
        if code in (401, 403):
            raise TimbreError(f"The speech service refused the key (HTTP {code}). Check GNANI_API_KEY and your credits.")
        raise TimbreError(f"The speech service rejected the request (HTTP {code}): {self._safe(body)[:200]}")

    def _log_first_429(self, body: str) -> None:
        path = self.cfg.tts_first_429_path
        try:
            if path.exists():
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n{self._safe(body)[:2000]}\n", encoding="utf-8")
        except OSError:
            pass


# --------------------------------------------------------------------------- remembering lines
class AudioCache:
    """Finished lines, in memory only, least recently used first out. The key includes the voice, language, speed
    and rate, so changing any of them cannot return the wrong audio."""

    def __init__(self, cfg: VoiceConfig) -> None:
        self.max_lines, self.max_bytes = cfg.tts_cache_max_lines, cfg.tts_cache_max_bytes
        self._items: OrderedDict[tuple, bytes] = OrderedDict()
        self._bytes = 0
        self.hits = self.misses = 0

    @staticmethod
    def key(cfg: VoiceConfig, text: str) -> tuple:
        return (text, cfg.timbre_model, cfg.tts_voice, cfg.tts_language, cfg.tts_speed, cfg.tts_sample_rate)

    def get(self, key: tuple) -> bytes | None:
        if key in self._items:
            self._items.move_to_end(key)
            self.hits += 1
            return self._items[key]
        self.misses += 1
        return None

    def put(self, key: tuple, pcm: bytes) -> None:
        if len(pcm) > self.max_bytes:
            return
        if key in self._items:
            self._bytes -= len(self._items.pop(key))
        self._items[key] = pcm
        self._bytes += len(pcm)
        while len(self._items) > self.max_lines or self._bytes > self.max_bytes:
            _, old = self._items.popitem(last=False)
            self._bytes -= len(old)

    def __len__(self) -> int:
        return len(self._items)


# --------------------------------------------------------------------------- one at a time
class _Waiter:
    def __init__(self, text: str, on_chunk: OnChunk, past_limit: bool) -> None:
        self.text, self.on_chunk, self.past_limit = text, on_chunk, past_limit
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()


class SpeechQueue:
    """Runs lines one at a time. If a newer line arrives while an older one is still waiting its turn, the older
    one is dropped (its caller gets None): by then it is out of date. The line already being spoken is not cut off."""

    def __init__(self, client: TimbreClient, cfg: VoiceConfig, ledger: Ledger, *, cache: AudioCache | None = None,
                 on_charge: Callable[[int], None] | None = None) -> None:
        self.client, self.cfg, self.ledger = client, cfg, ledger
        self.cache = cache if cache is not None else AudioCache(cfg)      # NOT "cache or ...": an empty cache has length 0 and counts as false
        self._on_charge = on_charge or self._charge_ledger
        self._waiting: _Waiter | None = None
        self._worker: asyncio.Task | None = None
        self.superseded = 0
        self.max_running = 0                         # most requests ever running at once (must stay 1)
        self._running = 0

    def _charge_ledger(self, chars: int) -> None:
        self.ledger.add(chars / 1000.0 * self.cfg.tts_credits_per_1000_chars, requests=1, note="text to speech")

    async def say(self, text: str, on_chunk: OnChunk, *, past_limit: bool = False) -> Synthesis | None:
        """`past_limit` lets ONE line through at the credit hard stop (the goodbye). It never lets one through when the
        whole pool is spent."""
        waiter = _Waiter(text, on_chunk, past_limit)
        if self._waiting is not None and not self._waiting.future.done():
            self._waiting.future.set_result(None)
            self.superseded += 1
        self._waiting = waiter
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())
        return await waiter.future

    async def _run(self) -> None:
        while self._waiting is not None:
            waiter, self._waiting = self._waiting, None
            try:
                waiter.future.set_result(await self._speak(waiter.text, waiter.on_chunk, waiter.past_limit))
            except BaseException as e:               # includes cancellation: the waiter must not hang
                if not waiter.future.done():
                    waiter.future.set_exception(e)
                if isinstance(e, asyncio.CancelledError):
                    raise

    async def _speak(self, text: str, on_chunk: OnChunk, past_limit: bool = False) -> Synthesis:
        t0 = time.monotonic()
        key = AudioCache.key(self.cfg, text)
        pcm = self.cache.get(key)
        if pcm is not None:
            for i in range(0, len(pcm), 9600):       # about 0.2 s per piece at 24 kHz, the way a live line arrives
                await on_chunk(pcm[i:i + 9600])
            return Synthesis(pcm, self.cfg.tts_sample_rate, 0, True, None, time.monotonic() - t0, 0)
        if self.ledger.spent >= self.cfg.pool_credits:
            raise CreditLimitReached("The whole credit pool is spent: nothing more will be spoken.")
        if self.ledger.blocked() and not past_limit:
            raise CreditLimitReached("Credit hard stop: no more speech will be requested. Check the dashboard and run "
                                     "'python -m mirage.credits sync <figure>'.")
        charged = {"done": False}

        def charge_once() -> None:
            if not charged["done"]:                  # billed when audio starts (a failed request with no audio is not charged)
                charged["done"] = True
                self._on_charge(len(text))

        self._running += 1
        self.max_running = max(self.max_running, self._running)
        try:
            pcm, first, attempts = await self.client.synthesize(text, on_chunk, charge_once)
        finally:
            self._running -= 1
        self.cache.put(key, pcm)
        return Synthesis(pcm, self.cfg.tts_sample_rate, len(text), False, first, time.monotonic() - t0, attempts)


# --------------------------------------------------------------------------- the only way to speak
class Speaker:
    """Speaks a ReplyPlan. It is built with the gate (no gate, no Speaker) and refuses a plan the gate did not return."""

    def __init__(self, gate: object, queue: SpeechQueue) -> None:
        self.gate = require_gate(gate)
        self.queue = queue

    async def say(self, plan: ReplyPlan, on_chunk: OnChunk, *, past_limit: bool = False) -> Synthesis | None:
        if not plan.gated:
            raise UngatedPlan("This reply did not come through the safety gate, so it will not be spoken.")
        if plan.needs_gate:
            raise UngatedPlan("The gate left this reply unresolved (needs_gate), so it will not be spoken.")
        if not plan.text.strip():
            raise UngatedPlan("The gate produced no text for this reply, so nothing is spoken.")
        return await self.queue.say(plan.text, on_chunk, past_limit=past_limit)
