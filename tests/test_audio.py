"""Audio plumbing: resampling keeps the sound, frames are exact, and the pacer never sends a burst."""
from __future__ import annotations

import asyncio
import math

import numpy as np
import pytest

from mirage.audio import Framer, Pacer, Resampler, frame_rms
from mirage.config import VoiceConfig

CFG = VoiceConfig()
FRAME_S = CFG.frame_bytes / 2 / CFG.sample_rate


def tone(freq: float, seconds: float, rate: int, amp: float = 0.5) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * math.pi * freq * t) * amp * 32767).astype("<i2").tobytes()


def dominant_frequency(pcm: bytes, rate: int) -> float:
    x = np.frombuffer(pcm, dtype="<i2").astype(float)
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.argmax(spectrum) * rate / len(x))


# ------------------------------------------------------------------ resampler
@pytest.mark.parametrize("rate", [48000, 44100, 16000])
def test_resampling_keeps_the_pitch_and_the_length(rate):
    out = Resampler(rate).process(tone(440, 1.0, rate))
    missing = 8000 - len(out) / 2                               # the filter holds back a little audio at the start
    assert 0 <= missing < 400                                   # under 50 ms: a constant delay, not drift
    assert abs(dominant_frequency(out, 8000) - 440) < 5


def test_chunked_resampling_matches_one_big_chunk():
    """State is kept between chunks, so cutting the input into 20 ms pieces changes nothing audible."""
    source = tone(600, 1.0, 48000)
    whole = np.frombuffer(Resampler(48000).process(source), dtype="<i2").astype(float)
    r = Resampler(48000)
    step = 960 * 2                                              # 20 ms of 48 kHz int16
    pieces = b"".join(r.process(source[i:i + step]) for i in range(0, len(source), step))
    parts = np.frombuffer(pieces, dtype="<i2").astype(float)
    n = min(len(whole), len(parts))
    assert abs(len(whole) - len(parts)) < 40
    assert np.max(np.abs(whole[200:n - 200] - parts[200:n - 200])) < 400     # same wave, no clicks at the joins


def test_8k_input_passes_through_untouched_and_a_torn_byte_is_dropped():
    pcm = tone(300, 0.1, 8000)
    assert Resampler(8000).process(pcm) == pcm
    assert Resampler(8000).process(pcm + b"\x01") == pcm


def test_a_silly_sample_rate_is_refused():
    with pytest.raises(ValueError):
        Resampler(7)


# ------------------------------------------------------------------ framer
def test_frames_are_exactly_1024_bytes_whatever_the_input_sizes():
    framer = Framer(1024)
    data = bytes(range(256)) * 20                               # 5,120 bytes
    frames = []
    for size in (100, 700, 33, 2000, 2287):
        frames += framer.push(data[:size])
        data = data[size:]
    assert frames and all(len(f) == 1024 for f in frames)
    assert len(frames) * 1024 + framer.pending_bytes == 100 + 700 + 33 + 2000 + 2287


def test_frames_keep_the_bytes_in_order():
    framer = Framer(4)
    out = framer.push(b"abcdef") + framer.push(b"ghijkl")
    assert b"".join(out) == b"abcdefghijkl"


def test_frame_rms():
    assert frame_rms(bytes(1024)) == 0.0
    assert 0.3 < frame_rms(tone(440, 0.064, 8000)) < 0.4


# ------------------------------------------------------------------ pacer (a fake clock, so these run instantly)
class FakeTime:
    def __init__(self) -> None:
        self.t = 100.0

    def now(self) -> float:
        return self.t

    async def sleep(self, d: float) -> None:
        self.t += d
        await asyncio.sleep(0)


def run_pacer(pacer: Pacer, after_send=None, count: int = 20) -> list[bytes]:
    sent: list[bytes] = []

    async def go() -> None:
        task = asyncio.create_task(pacer.run())
        while len(sent) < count:
            await asyncio.sleep(0)
        pacer.stop()
        await task

    original = pacer.send

    async def recording(frame: bytes) -> None:
        sent.append(frame)
        if after_send:
            after_send(len(sent))
        await original(frame)

    pacer.send = recording
    asyncio.run(go())
    return sent


def make_pacer(clock: FakeTime, cfg: VoiceConfig = CFG) -> Pacer:
    async def nothing(frame: bytes) -> None:
        return None
    return Pacer(nothing, cfg, now=clock.now, sleep=clock.sleep)


def test_one_frame_every_64_ms_on_a_steady_clock():
    clock = FakeTime()
    pacer = make_pacer(clock)
    for _ in range(30):
        pacer.push(tone(440, FRAME_S, 8000))
    run_pacer(pacer, count=25)
    gaps = np.diff(pacer.sent_times)
    assert len(gaps) >= 20
    assert np.allclose(gaps, FRAME_S, atol=1e-6)


def test_a_flood_of_audio_is_dropped_oldest_first_not_sent_in_a_burst():
    clock = FakeTime()
    pacer = make_pacer(clock)
    frames = [bytes([i]) * 1024 for i in range(40)]
    for f in frames:
        pacer.push(f)
    assert pacer.dropped_frames == 40 - pacer.max_backlog
    sent = run_pacer(pacer, count=pacer.max_backlog + 3)
    assert sent[0] == frames[40 - pacer.max_backlog]              # the oldest were discarded
    assert np.allclose(np.diff(pacer.sent_times), FRAME_S, atol=1e-6)


def test_a_stall_restarts_the_clock_instead_of_sending_a_catch_up_burst():
    clock = FakeTime()
    pacer = make_pacer(clock)
    for _ in range(40):
        pacer.push(tone(440, FRAME_S, 8000))

    def stall(n: int) -> None:
        if n == 5:
            clock.t += 1.5                                      # the whole server froze for 1.5 s

    run_pacer(pacer, after_send=stall, count=15)
    gaps = np.diff(pacer.sent_times)
    assert pacer.resyncs == 1
    assert gaps.min() > FRAME_S * 0.99                          # no two frames closer than one frame: no burst


def test_an_empty_queue_sends_silent_filler_so_the_clock_keeps_running():
    clock = FakeTime()
    pacer = make_pacer(clock)
    sent = run_pacer(pacer, count=10)
    assert all(f == bytes(1024) for f in sent)
    assert pacer.filler_frames == len(sent) and pacer.real_frames == 0


def test_muted_real_audio_is_replaced_by_silence_and_counted():
    clock = FakeTime()
    pacer = make_pacer(clock)
    pacer.muted = True
    for _ in range(5):
        pacer.push(tone(440, FRAME_S, 8000))
    sent = run_pacer(pacer, count=5)
    assert all(f == bytes(1024) for f in sent)
    assert pacer.muted_frames == 5 and pacer.real_frames == 0 and pacer.voiced_frames == 0


def test_only_loud_frames_count_as_voiced():
    clock = FakeTime()
    pacer = make_pacer(clock)
    for _ in range(4):
        pacer.push(tone(440, FRAME_S, 8000, amp=0.5))
    for _ in range(4):
        pacer.push(tone(440, FRAME_S, 8000, amp=0.0005))        # a very quiet room
    run_pacer(pacer, count=8)
    assert pacer.real_frames == 8 and pacer.voiced_frames == 4
