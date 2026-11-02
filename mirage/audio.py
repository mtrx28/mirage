"""Audio plumbing for the voice path: browser audio in, exact 8 kHz frames out at real-time pace.

Three small pieces:
  Resampler  turns the browser's audio (usually 44.1 or 48 kHz) into 8 kHz 16-bit mono, remembering state
             between chunks so there are no clicks at the joins.
  Framer     cuts a stream of bytes into exact 1,024-byte frames (512 samples, 64 ms at 8 kHz).
  Pacer      sends one frame every 64 ms on a fixed clock. It never sends faster to catch up: Prisma's docs say
             bursts hurt its end-of-speech detection. If the browser is late it sends a silent frame; if audio
             piles up it drops the OLDEST audio instead of bursting.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Awaitable, Callable

import numpy as np
import soxr

from mirage.config import VoiceConfig

TARGET_RATE = 8000
# "MQ" holds back about 35 ms of audio at the start (the filter's delay; the default setting held back about 70 ms
# and the highest 130 ms). It is a constant delay, not drift. Plenty good for telephone-band speech.
RESAMPLE_QUALITY = "MQ"
SUPPORTED_INPUT_RATES = range(8000, 192001)


class Resampler:
    """int16 mono at `in_rate` -> int16 mono at 8 kHz, chunk by chunk."""

    def __init__(self, in_rate: int, out_rate: int = TARGET_RATE) -> None:
        if in_rate not in SUPPORTED_INPUT_RATES:
            raise ValueError(f"unsupported sample rate {in_rate}")
        self.in_rate, self.out_rate = in_rate, out_rate
        self._stream = None if in_rate == out_rate else soxr.ResampleStream(in_rate, out_rate, 1, dtype="int16", quality=RESAMPLE_QUALITY)

    def process(self, pcm: bytes) -> bytes:
        if len(pcm) % 2:
            pcm = pcm[:-1]                      # a torn sample can only be a transport error; drop the stray byte
        samples = np.frombuffer(pcm, dtype="<i2")
        if self._stream is None:
            return samples.tobytes()
        return self._stream.resample_chunk(samples).astype("<i2").tobytes()


class Framer:
    """Collects bytes and hands back exact fixed-size frames."""

    def __init__(self, frame_bytes: int) -> None:
        self.frame_bytes = frame_bytes
        self._buffer = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        self._buffer += data
        frames = []
        while len(self._buffer) >= self.frame_bytes:
            frames.append(bytes(self._buffer[:self.frame_bytes]))
            del self._buffer[:self.frame_bytes]
        return frames

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)


def frame_rms(frame: bytes) -> float:
    """Loudness of one frame, 0.0 (silent) to 1.0 (full scale)."""
    samples = np.frombuffer(frame, dtype="<i2").astype(np.float64) / 32768.0
    return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0


SendFrame = Callable[[bytes], Awaitable[None]]


class Pacer:
    """One frame out every `frame_s` seconds, on a fixed clock, no matter how audio arrives."""

    def __init__(self, send: SendFrame, cfg: VoiceConfig | None = None, *,
                 now: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.cfg = cfg or VoiceConfig()
        self.send, self.now, self.sleep = send, now, sleep
        self.frame_s = self.cfg.frame_bytes / 2 / self.cfg.sample_rate          # 0.064 s
        self.max_backlog = max(1, round(self.cfg.backlog_limit_s / self.frame_s))
        self._queue: deque[bytes] = deque()
        self._silence = bytes(self.cfg.frame_bytes)
        self.muted = False                  # half-duplex (stop 3): real audio is replaced by silence while Uncle speaks
        self._stop = False
        # counters, for the log and the tests
        self.real_frames = 0                # real microphone frames sent
        self.voiced_frames = 0              # of those, loud enough to count as speech
        self.filler_frames = 0              # silent frames sent because the browser had nothing ready
        self.muted_frames = 0               # real frames replaced by silence while muted
        self.dropped_frames = 0             # old frames discarded because audio arrived faster than real time
        self.resyncs = 0                    # times the clock restarted after a long stall
        self.sent_times: list[float] = []   # when each frame went out (kept short; for the log's timing summary)

    def push(self, frame: bytes) -> None:
        self._queue.append(frame)
        while len(self._queue) > self.max_backlog:
            self._queue.popleft()
            self.dropped_frames += 1

    @property
    def frames_sent(self) -> int:
        return self.real_frames + self.filler_frames + self.muted_frames

    @property
    def stream_seconds(self) -> float:
        """How much audio has gone to Prisma, in seconds of stream time."""
        return self.frames_sent * self.frame_s

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        next_t = self.now()
        while not self._stop:
            frame = self._queue.popleft() if self._queue else None
            if self.muted:
                out = self._silence
                if frame is not None:
                    self.muted_frames += 1
                else:
                    self.filler_frames += 1
            elif frame is None:
                out = self._silence
                self.filler_frames += 1
            else:
                out = frame
                self.real_frames += 1
                if frame_rms(frame) >= self.cfg.voiced_rms:
                    self.voiced_frames += 1
            await self.send(out)
            if len(self.sent_times) < 100000:
                self.sent_times.append(self.now())
            next_t += self.frame_s
            delay = next_t - self.now()
            if -delay > self.cfg.resync_after_s:
                # Badly late (the loop stalled). Do NOT send a burst to catch up: restart the clock.
                self.resyncs += 1
                next_t = self.now() + self.frame_s
                delay = self.frame_s
            await self.sleep(max(delay, 0))     # a small lateness is absorbed by the next frame's shorter wait
