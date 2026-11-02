"""Stop 1 of the voice path: mic audio to Prisma and back, the ledger, the limits, and the gate-required rule.

No test here touches the real Gnani service. Prisma is played by a small WebSocket server inside the test
(it records the headers and frames it receives), and the browser by a list that collects what the server sends.
"""
from __future__ import annotations

import asyncio
import ast
import json
import math
import os
import time
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from mirage import server as server_module
from mirage.calllog import purge_old_audio
from mirage.config import ROOT, VoiceConfig
from mirage.credits import CreditLimitReached, Ledger
from mirage.prisma import MAX_KEYTERMS, PrismaError, PrismaLink, build_headers, load_keyterms
from mirage.safety import Gate
from mirage.server import ProcessLock, create_app
from mirage.session import CallSession, GateRequired, require_gate

KEY = "SECRETKEY123"


def passthrough_gate(plan, state, context):
    return plan


def make_cfg(tmp_path: Path, **overrides) -> VoiceConfig:
    return replace(VoiceConfig(), calls_dir=tmp_path / "calls", audio_dir=tmp_path / "audio",
                   ledger_path=tmp_path / "credits.json", lock_path=tmp_path / "server.lock",
                   tick_s=0.05, **overrides)


def tone_chunk(seconds: float, rate: int = 48000, amp: float = 0.5, start: float = 0.0) -> bytes:
    t = start + np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * math.pi * 440 * t) * amp * 32767).astype("<i2").tobytes()


# ------------------------------------------------------------------ a stand-in for Prisma
class FakePrisma:
    def __init__(self, reply_after: int = 5, close_after: int | None = None, close_code: int = 1011,
                 refuse_with: HTTPStatus | None = None) -> None:
        self.headers: list = []
        self.frames: list[bytes] = []
        self.connections = 0
        self.reply_after, self.close_after, self.close_code, self.refuse_with = reply_after, close_after, close_code, refuse_with

    async def handler(self, conn) -> None:
        self.connections += 1
        mine = self.connections
        self.headers.append(conn.request.headers)
        await conn.send(json.dumps({"type": "connected", "request_id": f"req-{mine}",
                                    "config": {"sample_rate": 16000, "language_code": "hi-IN"}}))
        count = 0
        try:
            async for message in conn:
                if not isinstance(message, bytes):
                    continue
                self.frames.append(message)
                count += 1
                if count == self.reply_after:
                    await conn.send(json.dumps({"type": "speech_start", "utterance_id": "u1"}))
                    await conn.send(json.dumps({"type": "processing", "utterance_id": "u1"}))
                    await conn.send(json.dumps({"type": "transcript", "text": "haan ji boliye", "is_final": True,
                                                "audio_duration_ms": 1200, "latency": 300, "segment_id": "s1",
                                                "utterance_id": "u1", "detected_language_code": "hi-IN"}))
                if self.close_after is not None and mine == 1 and count == self.close_after:
                    await conn.close(self.close_code)
                    return
        except ConnectionClosed:
            pass

    async def deny(self, conn, request):
        if self.refuse_with is not None:
            return conn.respond(self.refuse_with, "no\n")
        return None


def with_fake_prisma(fake: FakePrisma, body):
    """Run `body(url)` while the fake Prisma listens on a free local port."""
    async def go():
        async with serve(fake.handler, "127.0.0.1", 0, process_request=fake.deny) as srv:
            port = list(srv.sockets)[0].getsockname()[1]
            return await body(f"ws://127.0.0.1:{port}")
    return asyncio.run(go())


# ------------------------------------------------------------------ headers and keyterms
def test_headers_are_the_measured_set_and_there_is_no_forced_speech_cap():
    h = build_headers(VoiceConfig(), KEY)
    assert h["model"] == "prisma-v3.0" and h["language_code"] == "hi-IN" and h["format"] == "codemix"
    assert h["x-sample-rate"] == "8000" and h["x-min-silence-ms"] == "500" and h["x-api-key-id"] == KEY
    assert "x-max-speech-s" not in h and "keyterms" not in h


def test_keyterms_go_as_an_ascii_json_array_only_when_given():
    terms = load_keyterms()
    h = build_headers(VoiceConfig(), KEY, terms)
    assert json.loads(h["keyterms"]) == terms
    assert h["keyterms"].isascii()


def test_the_real_keyterm_list_is_valid():
    terms = load_keyterms()
    assert 0 < len(terms) <= MAX_KEYTERMS and "AnyDesk" in terms and "CBI" in terms


@pytest.mark.parametrize("bad", [["नमस्ते"], ["a,b"], ["CBI", "cbi"], ['say "hi"'], [f"t{i}" for i in range(201)]])
def test_a_bad_keyterm_list_is_refused(tmp_path, bad):
    path = tmp_path / "k.json"
    path.write_text(json.dumps({"terms": bad}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_keyterms(path)


# ------------------------------------------------------------------ the Prisma link, against the fake
def test_the_link_sends_the_right_headers_and_exact_frames():
    fake = FakePrisma(reply_after=3)
    got: list[dict] = []

    async def body(url):
        cfg = replace(VoiceConfig(), prisma_url=url)

        async def on_message(m): got.append(m)

        async def on_closed(r): got.append({"type": "closed", "reason": r})

        link = PrismaLink(cfg, KEY, on_message, on_closed, keyterms=["AnyDesk"])
        await link.open()
        for _ in range(4):
            await link.send_frame(bytes(1024))
        await asyncio.sleep(0.3)
        await link.close()

    with_fake_prisma(fake, body)
    h = fake.headers[0]
    assert h["x-api-key-id"] == KEY and h["model"] == "prisma-v3.0" and h["format"] == "codemix"
    assert h["x-min-silence-ms"] == "500" and h["x-sample-rate"] == "8000" and h["language_code"] == "hi-IN"
    assert json.loads(h["keyterms"]) == ["AnyDesk"]
    assert "x-max-speech-s" not in h
    assert [len(f) for f in fake.frames] == [1024] * 4
    assert [m["type"] for m in got][:4] == ["connected", "speech_start", "processing", "transcript"]


def test_a_refused_key_gives_a_readable_error_without_the_key():
    fake = FakePrisma(refuse_with=HTTPStatus.UNAUTHORIZED)

    async def body(url):
        async def noop(m): pass
        link = PrismaLink(replace(VoiceConfig(), prisma_url=url), KEY, noop, noop)
        with pytest.raises(PrismaError) as error:
            await link.open()
        return str(error.value)

    message = with_fake_prisma(fake, body)
    assert "401" in message and "GNANI_API_KEY" in message and KEY not in message


def test_an_unreachable_service_gives_a_readable_error():
    async def go():
        async def noop(m): pass
        link = PrismaLink(replace(VoiceConfig(), prisma_url="ws://127.0.0.1:1", connect_timeout_s=2), KEY, noop, noop)
        with pytest.raises(PrismaError) as error:
            await link.open()
        return str(error.value)
    assert "Could not reach Prisma" in asyncio.run(go())


def test_a_dropped_connection_is_retried_once_without_replaying_audio():
    fake = FakePrisma(reply_after=99, close_after=2, close_code=1011)
    got: list[dict] = []

    async def body(url):
        cfg = replace(VoiceConfig(), prisma_url=url, reconnect_delay_s=0.05)

        async def on_message(m): got.append(m)

        async def on_closed(r): got.append({"type": "closed", "reason": r})

        link = PrismaLink(cfg, KEY, on_message, on_closed)
        await link.open()
        for _ in range(8):
            await link.send_frame(bytes(1024))
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        await link.close()
        return link

    link = with_fake_prisma(fake, body)
    assert fake.connections == 2 and link.reconnects == 1
    assert any(m.get("reconnected") for m in got)
    assert not any(m["type"] == "closed" for m in got)


def test_the_fifteen_minute_close_is_not_retried():
    fake = FakePrisma(reply_after=99, close_after=2, close_code=1001)
    got: list[dict] = []

    async def body(url):
        cfg = replace(VoiceConfig(), prisma_url=url, reconnect_delay_s=0.05)

        async def on_message(m): got.append(m)

        async def on_closed(r): got.append({"type": "closed", "reason": r})

        link = PrismaLink(cfg, KEY, on_message, on_closed)
        await link.open()
        for _ in range(6):
            await link.send_frame(bytes(1024))
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        await link.close()

    with_fake_prisma(fake, body)
    assert fake.connections == 1
    assert any(m["type"] == "closed" and "1001" in m["reason"] for m in got)


# ------------------------------------------------------------------ a whole session against the fake
def run_session(tmp_path, fake, *, seconds=0.9, ledger_spent=None, gate=passthrough_gate, max_call_s=None,
                cfg_changes=None, hangup=True):
    cfg_changes = cfg_changes or {}
    notes: list[dict] = []
    result = {}

    async def body(url):
        cfg = make_cfg(tmp_path, prisma_url=url, **cfg_changes)
        ledger = Ledger(cfg)
        if ledger_spent is not None:
            ledger.sync(ledger_spent)

        async def notify(m): notes.append(m)

        session = CallSession(cfg, gate, ledger, notify, key=KEY, keyterms=["AnyDesk"], max_call_s=max_call_s)
        result["session"], result["ledger"], result["cfg"] = session, ledger, cfg
        try:
            await session.start(48000)
        except Exception as e:
            result["error"] = e
            return
        end = time.monotonic() + seconds
        position = 0.0
        while time.monotonic() < end and not session.done.is_set():
            session.feed(tone_chunk(0.02, start=position))
            position += 0.02
            await asyncio.sleep(0.02)
        if hangup:
            await session.hangup()
        await asyncio.wait_for(session.done.wait(), 5)

    with_fake_prisma(fake, body)
    return result, notes


def test_a_call_streams_frames_logs_a_transcript_and_charges_the_ledger(tmp_path):
    fake = FakePrisma(reply_after=5)
    result, notes = run_session(tmp_path, fake)
    session, ledger, cfg = result["session"], result["ledger"], result["cfg"]

    kinds = [n["type"] for n in notes]
    assert kinds[0] == "ready" and "transcript" in kinds and kinds[-1] == "ended"
    assert next(n for n in notes if n["type"] == "transcript")["text"] == "haan ji boliye"
    assert len(fake.frames) >= 8 and all(len(f) == 1024 for f in fake.frames)
    assert session.end_reason == "hangup"

    # the ledger moved by this call's estimate, and counted one request
    assert ledger.requests == 24 and ledger.spent > 18.37
    assert abs(ledger.spent - 18.37 - session.meter.credits) < 1e-6
    assert session.meter.reported_s == pytest.approx(1.2)

    # evidence on disk
    folder = cfg.calls_dir / session.log.call_id
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    assert summary["end_reason"] == "hangup" and summary["transcripts"] == 1
    assert summary["prisma"]["request_ids"] == ["req-1"]
    events = [json.loads(l)["event"] for l in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {"start", "prisma_connected", "speech_start", "processing", "end"} <= set(events)
    lines = (folder / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(l)["text"] for l in lines] == ["haan ji boliye"]


def test_the_api_key_is_never_written_to_disk_or_sent_to_the_page(tmp_path):
    result, notes = run_session(tmp_path, FakePrisma(reply_after=3))
    texts = [json.dumps(n) for n in notes]
    for path in (tmp_path / "calls").rglob("*"):
        if path.is_file():
            texts.append(path.read_text(encoding="utf-8"))
    texts.append((tmp_path / "credits.json").read_text(encoding="utf-8"))
    assert not any(KEY in t for t in texts)


def test_no_raw_audio_is_saved_by_default_and_saved_audio_is_deleted_on_schedule(tmp_path, monkeypatch):
    monkeypatch.delenv("MIRAGE_SAVE_AUDIO", raising=False)
    run_session(tmp_path, FakePrisma(reply_after=3), seconds=0.3)
    assert not (tmp_path / "audio").exists()

    monkeypatch.setenv("MIRAGE_SAVE_AUDIO", "1")
    result, _ = run_session(tmp_path, FakePrisma(reply_after=3), seconds=0.3)
    wavs = list((tmp_path / "audio").rglob("*.wav"))
    assert len(wavs) == 1 and wavs[0].stat().st_size > 1000
    assert purge_old_audio(result["cfg"]) == []                              # fresh: kept
    folder = wavs[0].parent
    old = time.time() - 8 * 86400
    os.utime(folder, (old, old))
    assert purge_old_audio(result["cfg"]) == [folder.name]                   # older than 7 days: deleted
    assert not folder.exists()


def test_the_hard_stop_refuses_to_start_a_call_and_never_opens_prisma(tmp_path):
    fake = FakePrisma()
    result, notes = run_session(tmp_path, fake, ledger_spent=4000.0, hangup=False)
    assert isinstance(result["error"], CreditLimitReached)
    assert fake.connections == 0 and result["ledger"].spent == 4000.0
    assert result["session"].end_reason == "credit_limit_at_start"


def test_the_hard_stop_ends_a_call_in_progress(tmp_path):
    fake = FakePrisma(reply_after=99)
    # one credit per second of speech, so the 4,000 line is crossed within a fraction of a second
    result, notes = run_session(tmp_path, fake, ledger_spent=3999.8, seconds=3, hangup=False,
                                cfg_changes={"stt_credits_per_min": 60.0})
    assert result["session"].end_reason == "credit_limit"
    assert notes[-1]["type"] == "ended" and notes[-1]["reason"] == "credit_limit"
    assert result["ledger"].spent >= 4000.0 - 1e-6                           # what was spent is recorded


def test_the_per_call_cap_ends_the_call(tmp_path):
    result, _ = run_session(tmp_path, FakePrisma(reply_after=99), seconds=3, hangup=False,
                            cfg_changes={"stt_credits_per_min": 60.0, "per_call_cap_credits": 0.2})
    assert result["session"].end_reason == "call_credit_cap"


def test_the_time_limit_ends_the_call(tmp_path):
    result, notes = run_session(tmp_path, FakePrisma(reply_after=99), seconds=3, hangup=False, max_call_s=0.3)
    assert result["session"].end_reason == "time_limit"
    assert notes[-1]["type"] == "ended" and 0.3 <= notes[-1]["held_s"] < 1.5


def test_when_prisma_closes_for_good_the_call_ends(tmp_path):
    fake = FakePrisma(reply_after=99, close_after=3, close_code=1001)
    result, notes = run_session(tmp_path, fake, seconds=3, hangup=False)
    assert result["session"].end_reason == "prisma_closed"
    assert notes[-1]["type"] == "ended"


def test_ticks_report_minutes_held_and_credits(tmp_path):
    result, notes = run_session(tmp_path, FakePrisma(reply_after=99), seconds=0.4)
    ticks = [n for n in notes if n["type"] == "tick"]
    assert ticks and {"held_s", "call_credits", "ledger_spent", "pool", "pool_fraction", "warning"} <= set(ticks[0])
    assert notes[-1]["type"] == "ended" and "call_credits" in notes[-1]


# ------------------------------------------------------------------ the gate is required
def test_no_gate_no_session(tmp_path):
    cfg = make_cfg(tmp_path)
    async def notify(m): pass
    with pytest.raises(GateRequired):
        CallSession(cfg, None, Ledger(cfg), notify, key=KEY)
    with pytest.raises(GateRequired):
        CallSession(cfg, object(), Ledger(cfg), notify, key=KEY)               # present but not a gate


def test_no_gate_no_server(tmp_path):
    cfg = make_cfg(tmp_path)
    with pytest.raises(GateRequired):
        create_app(None, cfg)
    with pytest.raises(GateRequired):
        create_app("a gate", cfg)
    create_app(Gate(), cfg)                                                  # the real gate is accepted


def test_the_server_command_builds_its_app_with_the_real_gate(monkeypatch, tmp_path):
    """main() must pass a Gate to create_app. We stop it right before uvicorn would start."""
    seen = {}
    real = server_module.create_app

    def spy(gate, *a, **kw):
        seen["gate"] = gate
        return real(gate, *a, **kw)

    monkeypatch.setattr(server_module, "create_app", spy)
    monkeypatch.setattr(server_module, "VoiceConfig", lambda: make_cfg(tmp_path))
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    server_module.main(["--keyterms", "off"])
    assert isinstance(seen["gate"], Gate)


def test_the_gate_check_itself_rejects_non_gates():
    for bad in (None, 0, "gate", object()):
        with pytest.raises(GateRequired):
            require_gate(bad)
    assert require_gate(passthrough_gate) is passthrough_gate


def ungated_brain_calls(source: str) -> list[int]:
    """Line numbers where CallBrain(...) is built without a real `gate=` argument."""
    bad = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name == "CallBrain":
                gate = next((k.value for k in node.keywords if k.arg == "gate"), None)
                if gate is None or (isinstance(gate, ast.Constant) and gate.value is None):
                    bad.append(node.lineno)
    return bad


def test_no_code_in_the_package_builds_a_brain_without_a_gate():
    problems = []
    for path in (ROOT / "mirage").glob("*.py"):
        if path.name == "fsm.py":                                            # where CallBrain is defined
            continue
        for line in ungated_brain_calls(path.read_text(encoding="utf-8")):
            problems.append(f"{path.name}:{line}")
    assert problems == []


def test_the_brain_gate_check_can_fail():
    assert ungated_brain_calls("brain = CallBrain(config=c)") == [1]
    assert ungated_brain_calls("brain = CallBrain(gate=None)") == [1]
    assert ungated_brain_calls("brain = CallBrain(gate=Gate())") == []


# ------------------------------------------------------------------ the web server
class FakeLink:
    """Stands in for Prisma inside the web-server tests (those run in another thread and event loop)."""
    instances: list = []

    def __init__(self, cfg, key, on_message, on_closed, keyterms=None):
        self.on_message, self.frames, self.keyterms = on_message, [], keyterms
        self.request_ids, self.reconnects, self.frames_lost, self.closed = ["fake"], 0, 0, False
        self.sent_header_names, self.keyterms_header_chars = ["model"], 0
        FakeLink.instances.append(self)

    async def open(self):
        await self.on_message({"type": "connected", "request_id": "fake"})

    async def send_frame(self, frame):
        self.frames.append(frame)
        if len(self.frames) == 3:
            await self.on_message({"type": "transcript", "text": "hello beta", "audio_duration_ms": 800})
            await self.on_message({"type": "error", "message": f"bad key {KEY}"})

    async def close(self):
        self.closed = True


ORIGIN = {"origin": "http://127.0.0.1:8000"}


def make_client(tmp_path, key=KEY, **kw):
    FakeLink.instances.clear()
    cfg = make_cfg(tmp_path)
    app = create_app(passthrough_gate, cfg, key_loader=lambda: key, link_factory=FakeLink, **kw)
    return TestClient(app), cfg


def read_until(ws, kind: str, limit: int = 60) -> list[dict]:
    seen = []
    for _ in range(limit):
        message = ws.receive_json()
        seen.append(message)
        if message["type"] == kind:
            return seen
    raise AssertionError(f"never saw {kind}: {[m['type'] for m in seen]}")


def test_a_full_call_through_the_web_socket(tmp_path):
    client, cfg = make_client(tmp_path)
    with client.websocket_connect("/ws/call", headers=ORIGIN) as ws:
        ws.send_json({"type": "start", "sample_rate": 48000})
        assert read_until(ws, "ready")[-1]["max_call_s"] == cfg.max_call_s
        for i in range(30):                                   # about 0.6 s of microphone audio
            ws.send_bytes(tone_chunk(0.02, start=i * 0.02))
            time.sleep(0.02)
        seen = read_until(ws, "transcript")
        assert seen[-1]["text"] == "hello beta"
        ws.send_json({"type": "hangup"})
        ended = read_until(ws, "ended")
    notices = json.dumps([m for m in seen + ended])
    assert KEY not in notices                                # the key Prisma "echoed" back was scrubbed
    assert "***" in notices
    assert ended[-1]["reason"] == "hangup"
    assert all(len(f) == 1024 for f in FakeLink.instances[0].frames) and FakeLink.instances[0].closed
    assert FakeLink.instances[0].keyterms is None


def test_a_page_from_another_origin_cannot_start_a_call(tmp_path):
    client, _ = make_client(tmp_path)
    for headers in ({"origin": "http://evil.example"}, {"origin": "http://127.0.0.1:9999"}, {}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/call", headers=headers):
                pass
    assert FakeLink.instances == []


def test_only_one_call_at_a_time(tmp_path):
    client, _ = make_client(tmp_path)
    with client.websocket_connect("/ws/call", headers=ORIGIN) as first:
        first.send_json({"type": "start", "sample_rate": 48000})
        read_until(first, "ready")
        with client.websocket_connect("/ws/call", headers=ORIGIN) as second:
            assert "already running" in second.receive_json()["message"]
        first.send_json({"type": "hangup"})
        read_until(first, "ended")


def test_a_missing_key_is_reported_in_plain_words(tmp_path):
    client, _ = make_client(tmp_path, key="")
    with client.websocket_connect("/ws/call", headers=ORIGIN) as ws:
        ws.send_json({"type": "start", "sample_rate": 48000})
        assert ".env" in ws.receive_json()["message"]
    assert FakeLink.instances == []


def test_a_call_is_refused_at_the_hard_stop_with_a_readable_message(tmp_path):
    client, cfg = make_client(tmp_path)
    Ledger(cfg).sync(4000.0)
    client.app.state.ledger.sync(4000.0)
    with client.websocket_connect("/ws/call", headers=ORIGIN) as ws:
        ws.send_json({"type": "start", "sample_rate": 48000})
        messages = [ws.receive_json() for _ in range(2)]
    assert any("hard stop" in m.get("message", "").lower() for m in messages)
    assert FakeLink.instances == []


def test_the_page_has_the_banner_hang_up_and_mic_help(tmp_path):
    client, _ = make_client(tmp_path)
    page = client.get("/")
    assert page.status_code == 200 and "STAGED DEMO: synthetic data" in page.text and "Hang up" in page.text
    js = client.get("/static/app.js")
    assert js.status_code == 200 and "NotAllowedError" in js.text and "NotFoundError" in js.text
    assert client.get("/static/capture-worklet.js").status_code == 200
    assert client.get("/static/..%2Fserver.py").status_code == 404
    assert client.get("/static/secrets.txt").status_code == 404
    assert "no-store" in page.headers["cache-control"]


def test_the_status_endpoint_never_includes_the_key(tmp_path):
    client, _ = make_client(tmp_path)
    status = client.get("/api/status").json()
    assert status["pool"] == 5000.0 and status["hard_stop"] == 4000.0 and KEY not in json.dumps(status)


def test_the_server_refuses_any_address_but_this_computer():
    for host in ("0.0.0.0", "192.168.1.5", "example.com"):
        with pytest.raises(SystemExit):
            server_module.main(["--host", host])


def test_only_one_server_process_may_hold_the_lock(tmp_path):
    first, second = ProcessLock(tmp_path / "x.lock"), ProcessLock(tmp_path / "x.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_the_page_code_has_no_dialer_and_no_outside_addresses():
    """The page talks only to the server that served it. (The repo-wide dialer scan is in test_no_outbound_calls.)"""
    text = " ".join((ROOT / "mirage" / "web" / name).read_text(encoding="utf-8")
                    for name in ("index.html", "app.js", "capture-worklet.js"))
    assert "http://" not in text.replace("http://127.0.0.1:8000", "").replace("http://localhost:8000", "")
    assert "https://" not in text
    assert "ws://" in text and "location.host" in text
    assert "tel:" not in text and "RTCPeerConnection" not in text


# ------------------------------------------------------------------ evidence added after the first real runs
def test_the_log_names_the_headers_sent_and_keeps_the_raw_transcript_message(tmp_path):
    fake = FakePrisma(reply_after=3)
    result, _ = run_session(tmp_path, fake, seconds=0.5)                  # this helper passes keyterms=["AnyDesk"]
    folder = result["cfg"].calls_dir / result["session"].log.call_id
    events = [json.loads(l) for l in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    sent = next(e for e in events if e["event"] == "prisma_headers_sent")
    assert "keyterms" in sent["names"] and sent["keyterms_header_chars"] > 0
    assert KEY not in json.dumps(sent)                                    # names only, never values
    first = json.loads((folder / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["raw"]["type"] == "transcript" and first["raw"]["is_final"] is True


def test_without_keyterms_the_log_shows_no_keyterms_header(tmp_path):
    async def body(url):
        cfg = make_cfg(tmp_path, prisma_url=url)

        async def notify(m): pass

        session = CallSession(cfg, passthrough_gate, Ledger(cfg), notify, key=KEY, keyterms=None)
        await session.start(48000)
        await session.hangup()
        return cfg, session

    cfg, session = with_fake_prisma(FakePrisma(), body)
    events = [json.loads(l) for l in (cfg.calls_dir / session.log.call_id / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    sent = next(e for e in events if e["event"] == "prisma_headers_sent")
    assert "keyterms" not in sent["names"] and sent["keyterms_header_chars"] == 0


def test_the_ended_message_carries_the_pool_figures_the_page_needs(tmp_path):
    result, notes = run_session(tmp_path, FakePrisma(reply_after=99), seconds=0.3)
    ended = notes[-1]
    assert ended["type"] == "ended" and ended["pool"] == 5000.0
    assert 0 < ended["pool_fraction"] < 0.01 and ended["warning"] is False


def test_the_page_shows_the_pool_when_a_call_ends_and_never_a_half_filled_line():
    """Runs the page's own code in Node with a stub page (skipped if Node is not installed)."""
    import shutil
    import subprocess
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    script = r"""
    const fs = require("fs"), vm = require("vm");
    const store = {};
    const el = (id) => store[id] || (store[id] = { id, textContent: "", style: {}, disabled: false, addEventListener() {},
                                                  appendChild() {}, scrollTop: 0, scrollHeight: 0 });
    const ctx = vm.createContext({ document: { getElementById: el, createElement: () => ({ appendChild() {} }),
                                               createTextNode: () => ({}) },
                                   window: { addEventListener() {}, isSecureContext: true },
                                   navigator: {}, fetch: () => Promise.reject(new Error("no network")),
                                   setTimeout, location: { host: "x" }, WebSocket: { OPEN: 1 } });
    vm.runInContext(fs.readFileSync("mirage/web/app.js", "utf8"), ctx);
    const send = (m) => vm.runInContext("onServerMessage(" + JSON.stringify(m) + ")", ctx);
    send({type: "tick", held_s: 5, call_credits: 0.1, ledger_spent: 18.5, pool: 5000, pool_fraction: 0.0037, warning: false});
    send({type: "ended", reason: "hangup", call_credits: 0.14, ledger_spent: 18.79, pool: 5000, pool_fraction: 0.0038,
          warning: false, held_s: 57, transcripts: 7});
    console.log(JSON.stringify({ledger: store.ledger.textContent, pool: String(store.pool.textContent), pct: store.poolPct.textContent,
                                call: store.callCredits.textContent}));
    // an "ended" message that lacks the pool must not blank out the line already on screen
    send({type: "ended", reason: "hangup", call_credits: 0.2, ledger_spent: 18.9, held_s: 60, transcripts: 8});
    console.log(JSON.stringify({ledger: store.ledger.textContent, pool: String(store.pool.textContent), pct: store.poolPct.textContent}));
    """
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=ROOT, timeout=30)
    assert out.returncode == 0, out.stderr
    first, second = [json.loads(line) for line in out.stdout.strip().splitlines()]
    assert first == {"ledger": "18.79", "pool": "5000", "pct": "0.4%", "call": "0.14"}
    assert second["pool"] == "5000" and second["pct"] == "0.4%"           # unchanged, not blanked
