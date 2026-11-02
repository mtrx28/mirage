"""Stop 2 of the voice path: text to Timbre to the speaker.

Nothing here touches the real Gnani service. Timbre is played by an in-process mock (httpx.MockTransport) that
records every request, so the 429 behaviour, the one-at-a-time rule and the cache can be tested without spending a
credit or provoking a real 429.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from mirage.config import ROOT, VoiceConfig
from mirage.credits import CreditLimitReached, Ledger
from mirage.fsm import CallBrain
from mirage.gaterequired import GateRequired
from mirage.safety import Gate
from mirage.server import create_app
from mirage.timbre import (AudioCache, Speaker, SpeechQueue, TimbreClient, TimbreError, TimbreUnavailable, UngatedPlan,
                           classify_429, strip_wav_header)
from mirage.voicetest import fixed_line_plan

KEY = "SECRETKEY123"
BUSY_BODY = json.dumps({"detail": {"error_code": "RATE_LIMITED", "status_code": 429,
                                   "message": "Only 1 active request is allowed at a time. Wait for your current request to finish before sending another."}})
QUOTA_BODY = json.dumps({"detail": {"error_code": "RATE_LIMITED", "message": "Insufficient credits for this request."}})


def make_cfg(tmp_path: Path, **overrides) -> VoiceConfig:
    return replace(VoiceConfig(), ledger_path=tmp_path / "credits.json", calls_dir=tmp_path / "calls",
                   audio_dir=tmp_path / "audio", lock_path=tmp_path / "server.lock",
                   tts_first_429_path=tmp_path / "first_429.txt", tts_log_path=tmp_path / "tts_log.jsonl", **overrides)


def pcm_bytes(seconds: float = 0.5, rate: int = 24000) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * math.pi * 300 * t) * 0.3 * 32767).astype("<i2").tobytes()


def sse(chunks: list[bytes], *, complete: bool = True, error: str | None = None) -> str:
    out = ["event: start", 'data: {"status": "streaming_started"}', ""]
    for i, c in enumerate(chunks):
        out += ["event: chunk", "data: " + json.dumps({"chunk_index": i, "audio": base64.b64encode(c).decode(), "is_final": False}), ""]
    if error:
        out += ["event: error", "data: " + json.dumps({"type": "error", "message": error}), ""]
    if complete:
        out += ["event: complete", 'data: {"chunk_index": %d, "audio": "", "is_final": true}' % len(chunks), ""]
    return "\n".join(out) + "\n"


def ok(chunks: list[bytes], **kw) -> httpx.Response:
    return httpx.Response(200, content=sse(chunks, **kw).encode(), headers={"content-type": "text/event-stream"})


class FakeTime:
    def __init__(self) -> None:
        self.t, self.sleeps = 1000.0, []

    def now(self) -> float:
        return self.t

    async def sleep(self, d: float) -> None:
        self.sleeps.append(d)
        self.t += d


class Mock:
    """A scripted Timbre. `script` is a list of responses (or callables), one per request; the last repeats."""

    def __init__(self, script) -> None:
        self.script, self.requests, self.running, self.max_running = list(script), [], 0, 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            item = self.script[min(len(self.requests) - 1, len(self.script) - 1)]
            return await item(request) if callable(item) else item
        finally:
            self.running -= 1

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def make_client(tmp_path, script, *, cfg=None, clock=None):
    cfg = cfg or make_cfg(tmp_path)
    mock = Mock(script)
    clock = clock or FakeTime()
    http = httpx.AsyncClient(transport=httpx.MockTransport(mock))
    return TimbreClient(cfg, KEY, http=http, sleep=clock.sleep, clock=clock.now), mock, clock, cfg


def run(coro):
    return asyncio.run(coro)


async def collect(client: TimbreClient, text: str = "Haan ji boliye beta."):
    pieces: list[bytes] = []

    async def on_chunk(b: bytes) -> None:
        pieces.append(b)

    result = await client.synthesize(text, on_chunk)
    return result, pieces


# ------------------------------------------------------------------ one request
def test_a_line_is_requested_with_the_chosen_voice_and_the_audio_streams_back(tmp_path):
    audio = pcm_bytes()
    client, mock, _, cfg = make_client(tmp_path, [ok([audio[:6000], audio[6000:]])])
    (pcm, first, attempts), pieces = run(collect(client))
    assert pcm == audio and b"".join(pieces) == audio and len(pieces) == 2
    assert attempts == 1 and first is not None and mock.max_running == 1
    body = mock.bodies[0]
    assert body["model"] == "timbre-v3.0" and body["voice"] == "Prakash" and body["language"] == "hi-en"
    assert body["audio_config"] == {"sample_rate": 24000, "num_channels": 1, "sample_width": 2,
                                    "encoding": "linear_pcm", "container": "raw"}
    assert mock.requests[0].headers["x-api-key-id"] == KEY and mock.requests[0].url == cfg.timbre_url


def test_pieces_that_end_mid_sample_are_realigned(tmp_path):
    audio = pcm_bytes(0.1)
    cuts = [audio[:1001], audio[1001:2500], audio[2500:]]                     # odd sizes
    client, _, _, _ = make_client(tmp_path, [ok(cuts)])
    (pcm, _, _), pieces = run(collect(client))
    assert pcm == audio and all(len(p) % 2 == 0 for p in pieces)


def test_a_wav_header_that_arrives_anyway_is_removed():
    audio = pcm_bytes(0.05)
    header = b"RIFF" + (len(audio) + 36).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little") + bytes(16) + b"data" + len(audio).to_bytes(4, "little")
    assert strip_wav_header(header + audio) == audio
    assert strip_wav_header(audio) == audio


def test_an_error_event_is_reported_without_the_key(tmp_path):
    client, _, _, _ = make_client(tmp_path, [ok([], complete=False, error=f"bad text {KEY}")])
    with pytest.raises(TimbreError) as e:
        run(collect(client))
    assert "bad text" in str(e.value) and KEY not in str(e.value)


def test_a_stream_cut_short_is_an_error_not_a_finished_line(tmp_path):
    client, _, _, _ = make_client(tmp_path, [ok([pcm_bytes(0.1)], complete=False)])
    with pytest.raises(TimbreError, match="cut short"):
        run(collect(client))


def test_text_that_is_empty_or_too_long_is_refused_before_any_request(tmp_path):
    client, mock, _, cfg = make_client(tmp_path, [ok([pcm_bytes()])])
    for text in ("", "   ", "x" * (cfg.tts_max_chars + 1)):
        with pytest.raises(TimbreError):
            run(collect(client, text))
    assert mock.requests == []


# ------------------------------------------------------------------ 429 and other failures (all mocked: no real 429)
def busy() -> httpx.Response:
    return httpx.Response(429, text=BUSY_BODY)


def test_a_429_is_retried_with_short_waits_then_succeeds(tmp_path):
    client, mock, clock, _ = make_client(tmp_path, [busy(), busy(), ok([pcm_bytes()])])
    (pcm, _, attempts), _ = run(collect(client))
    assert attempts == 3 and len(mock.requests) == 3 and clock.sleeps == [0.5, 1.0]
    assert len(pcm) > 0


def test_it_gives_up_after_two_retries_and_the_line_is_not_spoken(tmp_path):
    client, mock, clock, _ = make_client(tmp_path, [busy()])
    with pytest.raises(TimbreUnavailable):
        run(collect(client))
    assert len(mock.requests) == 3 and clock.sleeps == [0.5, 1.0]


def test_retry_after_is_honoured_when_it_fits_and_a_long_one_gives_up_at_once(tmp_path):
    slow = httpx.Response(429, text=BUSY_BODY, headers={"retry-after": "2"})
    client, mock, clock, _ = make_client(tmp_path, [slow, ok([pcm_bytes()])])
    run(collect(client))
    assert clock.sleeps == [2.0]
    long = httpx.Response(429, text=BUSY_BODY, headers={"retry-after": "30"})
    client, mock, clock, _ = make_client(tmp_path, [long])
    with pytest.raises(TimbreUnavailable):
        run(collect(client))
    assert len(mock.requests) == 1 and clock.sleeps == []                    # a line 30 s late is worthless


def test_an_out_of_credits_429_is_not_retried(tmp_path):
    client, mock, clock, _ = make_client(tmp_path, [httpx.Response(429, text=QUOTA_BODY)])
    with pytest.raises(TimbreError, match="credits"):
        run(collect(client))
    assert len(mock.requests) == 1 and clock.sleeps == []


def test_classifying_429_bodies():
    assert classify_429(BUSY_BODY) == "busy"
    assert classify_429(QUOTA_BODY) == "quota"
    assert classify_429("something else entirely") == "unknown"


def test_temporary_errors_are_retried_but_bad_requests_and_refused_keys_are_not(tmp_path):
    client, mock, _, _ = make_client(tmp_path, [httpx.Response(503, text="down"), ok([pcm_bytes()])])
    run(collect(client))
    assert len(mock.requests) == 2
    for status, words in ((400, "rejected"), (403, "refused the key"), (401, "refused the key")):
        client, mock, _, _ = make_client(tmp_path, [httpx.Response(status, text=f"nope {KEY}")])
        with pytest.raises(TimbreError, match=words) as e:
            run(collect(client))
        assert len(mock.requests) == 1 and KEY not in str(e.value)


def test_a_network_failure_before_any_audio_is_retried(tmp_path):
    async def boom(request):
        raise httpx.ConnectError(f"cannot connect {KEY}")

    client, mock, clock, _ = make_client(tmp_path, [boom, ok([pcm_bytes()])])
    run(collect(client))
    assert len(mock.requests) == 2 and clock.sleeps == [0.5]


def test_a_request_that_hangs_is_cut_off_and_retried(tmp_path):
    async def hang(request):
        await asyncio.sleep(5)

    cfg = make_cfg(tmp_path, tts_attempt_timeout_s=0.1)
    client, mock, _, _ = make_client(tmp_path, [hang, ok([pcm_bytes()])], cfg=cfg)
    started = time.monotonic()
    run(collect(client))
    assert len(mock.requests) == 2 and time.monotonic() - started < 3


class BreaksMidStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes) -> None:
        self.first = first

    async def __aiter__(self):
        yield sse([self.first], complete=False).encode()
        raise httpx.ReadError("the connection broke")


def test_a_failure_after_audio_started_is_not_retried(tmp_path):
    """Retrying would speak the start of the line twice."""
    broken = httpx.Response(200, stream=BreaksMidStream(pcm_bytes(0.1)))
    client, mock, _, _ = make_client(tmp_path, [broken, ok([pcm_bytes()])])
    with pytest.raises(TimbreError, match="broke part-way"):
        run(collect(client))
    assert len(mock.requests) == 1


def test_the_first_429_body_is_kept_once_with_the_key_blanked(tmp_path):
    body = BUSY_BODY.replace("Only", f"{KEY} Only")
    client, _, _, cfg = make_client(tmp_path, [httpx.Response(429, text=body), httpx.Response(429, text="second body"), ok([pcm_bytes()])])
    run(collect(client))
    saved = cfg.tts_first_429_path.read_text(encoding="utf-8")
    assert "Only 1 active request" in saved and KEY not in saved and "second body" not in saved


# ------------------------------------------------------------------ one at a time, newest wins
def make_queue(tmp_path, script, **cfg_changes):
    cfg = make_cfg(tmp_path, **cfg_changes)
    client, mock, clock, _ = make_client(tmp_path, script, cfg=cfg)
    ledger = Ledger(cfg)
    return SpeechQueue(client, cfg, ledger), mock, ledger, cfg


def test_only_one_request_runs_at_a_time_and_a_newer_line_replaces_one_still_waiting(tmp_path):
    state = {}

    async def slow(request):
        await state["gate"].wait()
        return ok([pcm_bytes(0.1)])

    async def go():
        state["gate"] = asyncio.Event()
        queue, mock, ledger, _ = make_queue(tmp_path, [slow])
        got = []

        async def sink(b): pass

        t1 = asyncio.create_task(queue.say("line one", sink))
        await asyncio.sleep(0.05)                       # line one is now being requested
        t2 = asyncio.create_task(queue.say("line two", sink))
        await asyncio.sleep(0.01)
        t3 = asyncio.create_task(queue.say("line three", sink))
        await asyncio.sleep(0.01)
        state["gate"].set()
        results = await asyncio.wait_for(asyncio.gather(t1, t2, t3), 5)       # a regression must fail, not hang
        return results, mock, queue

    results, mock, queue = run(go())
    assert results[0] is not None and results[1] is None and results[2] is not None      # line two was superseded
    assert [b["text"] for b in mock.bodies] == ["line one", "line three"]
    assert mock.max_running == 1 and queue.max_running == 1 and queue.superseded == 1


def test_a_repeated_line_is_served_from_memory_free_and_instant(tmp_path):
    queue, mock, ledger, cfg = make_queue(tmp_path, [ok([pcm_bytes(0.5)])])
    before = ledger.spent

    async def go():
        got1, got2 = [], []

        async def s1(b): got1.append(b)

        async def s2(b): got2.append(b)

        first = await queue.say("Haan ji boliye beta.", s1)
        second = await queue.say("Haan ji boliye beta.", s2)
        return first, second, got1, got2

    first, second, got1, got2 = run(go())
    assert len(mock.requests) == 1                                                  # the second never left the machine
    assert not first.cached and second.cached and second.chars == 0 and second.first_byte_s is None
    assert b"".join(got1) == b"".join(got2) == first.pcm == second.pcm
    assert all(len(p) <= 9600 for p in got2)
    assert ledger.spent == pytest.approx(before + 20 / 1000 * 2.7) and ledger.requests == 24   # charged once, one request
    assert queue.cache.hits == 1


def test_a_different_voice_speed_or_text_is_a_different_cache_entry(tmp_path):
    cfg = make_cfg(tmp_path)
    keys = {AudioCache.key(cfg, "a"), AudioCache.key(cfg, "b"), AudioCache.key(replace(cfg, tts_voice="Avinash"), "a"),
            AudioCache.key(replace(cfg, tts_speed=0.9), "a"), AudioCache.key(replace(cfg, tts_sample_rate=8000), "a")}
    assert len(keys) == 5


def test_the_cache_is_bounded_and_forgets_the_least_recently_used(tmp_path):
    cfg = make_cfg(tmp_path, tts_cache_max_lines=2)
    cache = AudioCache(cfg)
    for name in ("a", "b"):
        cache.put((name,), b"xx")
    cache.get(("a",))                                                              # a is now the most recent
    cache.put(("c",), b"xx")
    assert cache.get(("b",)) is None and cache.get(("a",)) == b"xx" and cache.get(("c",)) == b"xx"
    small = AudioCache(make_cfg(tmp_path, tts_cache_max_bytes=10))
    small.put(("big",), bytes(11))
    small.put(("s1",), bytes(6))
    small.put(("s2",), bytes(6))
    assert len(small) == 1 and small.get(("s2",)) == bytes(6)


def test_the_cache_lives_in_memory_only(tmp_path):
    queue, mock, ledger, cfg = make_queue(tmp_path, [ok([pcm_bytes(0.2)])])

    async def go():
        async def sink(b): pass
        await queue.say("keep this in memory", sink)

    run(go())
    files = {p.name for p in tmp_path.rglob("*") if p.is_file()}
    assert files <= {"credits.json"}                                                # no audio, no cache file


def test_a_failed_request_is_not_charged_and_a_retried_one_is_charged_once(tmp_path):
    queue, mock, ledger, _ = make_queue(tmp_path, [httpx.Response(503, text="down")])
    before = ledger.spent

    async def say(q, text):
        async def sink(b): pass
        return await q.say(text, sink)

    with pytest.raises(TimbreUnavailable):
        run(say(queue, "never spoken"))
    assert ledger.spent == before and ledger.requests == 23

    other = tmp_path / "second"
    other.mkdir()
    queue, mock, ledger, _ = make_queue(other, [busy(), ok([pcm_bytes(0.1)])])
    run(say(queue, "twelve chars"))
    assert ledger.spent == pytest.approx(18.37 + 12 / 1000 * 2.7) and ledger.requests == 24


def test_at_the_hard_stop_no_new_line_is_requested(tmp_path):
    queue, mock, ledger, _ = make_queue(tmp_path, [ok([pcm_bytes(0.1)])])
    ledger.sync(4000.0)

    async def go():
        async def sink(b): pass
        await queue.say("anything", sink)

    with pytest.raises(CreditLimitReached):
        run(go())
    assert mock.requests == []


# ------------------------------------------------------------------ the speaking path needs the gate
def gated_plan():
    return fixed_line_plan(Gate())


def test_no_gate_no_speaker(tmp_path):
    queue, _, _, _ = make_queue(tmp_path, [ok([pcm_bytes()])])
    for bad in (None, object(), "gate"):
        with pytest.raises(GateRequired):
            Speaker(bad, queue)


def test_a_plan_that_skipped_the_gate_is_never_spoken(tmp_path):
    queue, mock, _, _ = make_queue(tmp_path, [ok([pcm_bytes()])])
    speaker = Speaker(Gate(), queue)
    brain = CallBrain(seed=0)                                                       # no gate: the raw pass-through
    brain.hear("Hello?", 0.0)
    raw = brain.respond().plan

    async def go(plan):
        async def sink(b): pass
        return await speaker.say(plan, sink)

    assert raw.text and not raw.gated
    with pytest.raises(UngatedPlan, match="did not come through"):
        run(go(raw))
    assert mock.requests == []


def test_a_plan_the_gate_left_unresolved_or_empty_is_not_spoken(tmp_path):
    queue, mock, _, _ = make_queue(tmp_path, [ok([pcm_bytes()])])
    speaker = Speaker(Gate(), queue)
    good = gated_plan()

    async def go(plan):
        async def sink(b): pass
        return await speaker.say(plan, sink)

    with pytest.raises(UngatedPlan, match="needs_gate"):
        run(go(replace(good, needs_gate=True)))
    with pytest.raises(UngatedPlan, match="no text"):
        run(go(replace(good, text="  ")))
    assert mock.requests == []


def test_a_gated_plan_is_spoken_exactly_as_the_gate_returned_it(tmp_path):
    queue, mock, _, _ = make_queue(tmp_path, [ok([pcm_bytes(0.1)])])
    plan = gated_plan()
    assert plan.gated and plan.text

    async def go():
        async def sink(b): pass
        return await Speaker(Gate(), queue).say(plan, sink)

    result = run(go())
    assert result is not None and mock.bodies[0]["text"] == plan.text


def test_every_plan_the_gate_returns_is_stamped_including_a_disclosure_and_a_call_over():
    gate = Gate()
    brain = CallBrain(seed=0, gate=gate)
    brain.hear("Hello?", 0.0)
    assert brain.respond().plan.gated                                               # a plain pass
    brain.hear("are you a robot or a real person?", 10.0)
    disclosure = brain.respond().plan
    assert disclosure.gate_action == "disclose" and disclosure.gated and disclosure.end_call
    brain.hear("hello?", 20.0)
    over = brain.respond().plan
    assert over.gate_action == "call_over" and over.gated and over.text == ""


# ------------------------------------------------------------------ the web server route
class FakeTTS:
    instances: list = []

    def __init__(self, cfg, key):
        self.key, self.texts, self.closed = key, [], False
        self.pcm = pcm_bytes(0.5)
        FakeTTS.instances.append(self)

    async def synthesize(self, text, on_chunk, on_first_audio=None):
        self.texts.append(text)
        await on_chunk(self.pcm[:3001][: 3000])
        if on_first_audio:
            on_first_audio()
        await on_chunk(self.pcm[3000:])
        return self.pcm, 0.4, 1

    async def aclose(self):
        self.closed = True


ORIGIN = {"origin": "http://127.0.0.1:8000"}


def web_client(tmp_path, key=KEY):
    FakeTTS.instances.clear()
    cfg = make_cfg(tmp_path)
    app = create_app(Gate(), cfg, key_loader=lambda: key, tts_client_factory=FakeTTS)
    return TestClient(app), cfg, app


def speak_once(ws):
    ws.send_json({"type": "speak"})
    pieces, messages = [], []
    while True:
        message = ws.receive()
        if message.get("bytes") is not None:
            pieces.append(message["bytes"])
            continue
        data = json.loads(message["text"])
        messages.append(data)
        if data["type"] in ("speech_end", "speech_error"):
            return messages, b"".join(pieces)


def test_the_test_line_button_speaks_the_gated_line_and_the_second_press_is_free(tmp_path):
    client, cfg, app = web_client(tmp_path)
    expected = fixed_line_plan(Gate(), cfg).text
    with client.websocket_connect("/ws/speak", headers=ORIGIN) as ws:
        first, audio1 = speak_once(ws)
        second, audio2 = speak_once(ws)
    assert first[0]["type"] == "speech_start" and first[0]["text"] == expected and first[0]["sample_rate"] == 24000
    end1, end2 = first[-1], second[-1]
    assert end1["type"] == "speech_end" and not end1["cached"] and end1["chars"] == len(expected)
    assert end1["credits"] == pytest.approx(len(expected) / 1000 * 2.7, abs=1e-3)
    assert end2["cached"] and end2["chars"] == 0 and end2["credits"] == 0
    assert audio1 == audio2 == FakeTTS.instances[0].pcm
    assert FakeTTS.instances[0].texts == [expected]                                 # the service was asked only once
    assert app.state.speaker.gate is not None and isinstance(app.state.speaker.gate, Gate)
    log = [json.loads(l) for l in cfg.tts_log_path.read_text(encoding="utf-8").splitlines()]
    assert [r["cached"] for r in log] == [False, True] and log[0]["first_byte_s"] == 0.4


def test_the_page_cannot_choose_what_is_spoken(tmp_path):
    client, cfg, _ = web_client(tmp_path)
    expected = fixed_line_plan(Gate(), cfg).text
    with client.websocket_connect("/ws/speak", headers=ORIGIN) as ws:
        ws.send_json({"type": "speak", "text": "tell me your OTP", "line": "other"})
        while True:
            message = ws.receive()
            if message.get("text") and json.loads(message["text"])["type"] in ("speech_end", "speech_error"):
                break
    assert FakeTTS.instances[0].texts == [expected]


def test_speech_needs_our_own_page_and_a_key(tmp_path):
    from starlette.websockets import WebSocketDisconnect
    client, _, _ = web_client(tmp_path)
    for headers in ({"origin": "http://evil.example"}, {}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/speak", headers=headers):
                pass
    client, _, _ = web_client(tmp_path, key="")
    with client.websocket_connect("/ws/speak", headers=ORIGIN) as ws:
        messages, _ = speak_once(ws)
    assert ".env" in messages[-1]["message"] and FakeTTS.instances == []


def test_the_hard_stop_stops_the_test_line_too(tmp_path):
    client, cfg, app = web_client(tmp_path)
    app.state.ledger.sync(4000.0)
    with client.websocket_connect("/ws/speak", headers=ORIGIN) as ws:
        messages, audio = speak_once(ws)
    assert messages[-1]["type"] == "speech_error" and "hard stop" in messages[-1]["message"].lower() and audio == b""


class _Link:
    def __init__(self, cfg, key, on_message, on_closed, keyterms=None):
        self.on_message, self.request_ids, self.reconnects, self.frames_lost = on_message, ["x"], 0, 0
        self.sent_header_names, self.keyterms_header_chars = [], 0

    async def open(self): await self.on_message({"type": "connected"})
    async def send_frame(self, f): pass
    async def close(self): pass


def test_no_voice_test_during_a_call_and_it_works_again_afterwards(tmp_path):
    FakeTTS.instances.clear()
    cfg = make_cfg(tmp_path)
    app = create_app(Gate(), cfg, key_loader=lambda: KEY, tts_client_factory=FakeTTS, link_factory=_Link)
    client = TestClient(app)
    with client.websocket_connect("/ws/call", headers=ORIGIN) as call:
        call.send_json({"type": "start", "sample_rate": 48000})
        while call.receive_json()["type"] != "ready":
            pass
        with client.websocket_connect("/ws/speak", headers=ORIGIN) as ws:
            messages, _ = speak_once(ws)
            assert messages[-1]["type"] == "speech_error" and "running" in messages[-1]["message"]
        call.send_json({"type": "hangup"})
        while call.receive_json()["type"] != "ended":
            pass
    with client.websocket_connect("/ws/speak", headers=ORIGIN) as ws:
        messages, audio = speak_once(ws)
    assert messages[-1]["type"] == "speech_end" and audio


def test_the_speaking_client_is_closed_when_the_server_stops(tmp_path):
    client, _, app = web_client(tmp_path)
    with client:                                                                    # runs startup and shutdown
        with client.websocket_connect("/ws/speak", headers=ORIGIN) as ws:
            speak_once(ws)
    assert FakeTTS.instances[0].closed


# ------------------------------------------------------------------ the page's player (its own code, run in Node)
NODE_PLAYER_SCRIPT = r"""
const fs = require("fs"), vm = require("vm");
const el = () => ({ textContent: "", style: {}, disabled: false, addEventListener() {}, appendChild() {}, scrollTop: 0, scrollHeight: 0 });
const ctx = vm.createContext({ document: { getElementById: el, createElement: el, createTextNode: () => ({}) },
  window: { addEventListener() {}, isSecureContext: true }, navigator: {}, fetch: () => Promise.reject(new Error("x")),
  setTimeout, location: { host: "x" }, WebSocket: { OPEN: 1 }, AudioContext: function () {}, DataView, Uint8Array });
vm.runInContext(fs.readFileSync("mirage/web/app.js", "utf8"), ctx);
const audio = { currentTime: 10, sources: [],
  createBuffer(channels, length, rate) { const data = new Float32Array(length); return { duration: length / rate, length, getChannelData: () => data, data }; },
  createBufferSource() { const s = { start(t) { s.at = t; }, connect() {} }; audio.sources.push(s); return s; }, destination: {} };
ctx.audio = audio;
const out = {};
vm.runInContext(`
  var done = null;
  var p = makePlayer(audio, 24000, (info) => { done = info; });
  // 3 + 5 + 4 bytes: pieces end mid-sample, 12 bytes = 6 samples in all
  const bytes = new Uint8Array([0x00,0x40, 0xFF]);                  // 0x4000 (0.5) then half of a sample
  p.push(bytes.buffer);
  p.push(new Uint8Array([0x7F, 0x00,0xC0, 0x00,0x00]).buffer);      // completes 0x7FFF, then 0xC000 (-0.5), 0, half...
`, ctx);
out.sourcesAfterTwoPieces = audio.sources.length;
out.firstSamples = Array.from(audio.sources[0].buffer.data);
out.secondSamples = Array.from(audio.sources[1].buffer.data);
out.startsAt = audio.sources.map((s) => s.at);
audio.currentTime = 99;                                               // the next piece arrives long after the audio has played out
vm.runInContext(`p.push(new Uint8Array([0x00, 0x20]).buffer);`, ctx);
out.lateStart = audio.sources[2].at;
vm.runInContext(`p.end();`, ctx);
out.doneBeforeEnded = vm.runInContext("done", ctx);
audio.sources.forEach((s) => s.onended());
out.done = vm.runInContext("done", ctx);
console.log(JSON.stringify(out));
"""


def test_the_pages_player_realigns_odd_pieces_schedules_them_back_to_back_and_counts_gaps():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", NODE_PLAYER_SCRIPT], capture_output=True, text=True, cwd=ROOT, timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["firstSamples"] == [0.5]                                           # 0x4000, the odd byte was held back
    assert r["secondSamples"][0] == pytest.approx(32767 / 32768, abs=1e-4) and r["secondSamples"][1] == pytest.approx(-0.5)
    assert r["startsAt"][0] == pytest.approx(10.12) and r["startsAt"][1] == pytest.approx(10.12 + 1 / 24000)
    assert r["lateStart"] == pytest.approx(99.03)                               # restarted after a gap, not scheduled in the past
    assert r["doneBeforeEnded"] is None                                         # not finished while sources are still playing
    assert r["done"] == {"underruns": 1}


def test_the_page_has_the_voice_test_button_and_no_text_box():
    html = (ROOT / "mirage" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="speak"' in html and "Speak the test line" in html
    assert "<input" not in html and "<textarea" not in html                       # nothing to type: the server picks the line
