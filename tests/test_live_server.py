"""Stop 3 through the web server (live mode) and the page's own new code (run in Node).

The server tests use FastAPI's test client with a scripted Prisma and a fake Timbre. The page tests run mirage/web/app.js
inside Node with a small stand-in for the page, and are skipped if Node is not installed.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from mirage import server as server_module
from mirage.config import ROOT, VoiceConfig
from mirage.safety import Gate
from mirage.server import create_app

KEY = "SECRETKEY123"
ORIGIN = {"origin": "http://127.0.0.1:8000"}


def pcm_bytes(seconds=0.2, rate=24000):
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * math.pi * 300 * t) * 0.3 * 32767).astype("<i2").tobytes()


class TalkingLink:
    """A scripted Prisma: after 3 frames it 'hears' the caller say something."""
    instances: list = []

    def __init__(self, cfg, key, on_message, on_closed, keyterms=None):
        self.on_message, self.frames = on_message, []
        self.request_ids, self.reconnects, self.frames_lost = ["x"], 0, 0
        self.sent_header_names, self.keyterms_header_chars = [], 0
        self.said = False
        TalkingLink.instances.append(self)

    async def open(self):
        await self.on_message({"type": "connected"})

    async def send_frame(self, frame):
        self.frames.append(frame)
        if len(self.frames) == 3 and not self.said:
            self.said = True
            await self.on_message({"type": "speech_start", "utterance_id": "u1"})
            await self.on_message({"type": "transcript", "utterance_id": "u1", "text": "main CBI se bol raha hoon case number hai chaar saat do nau ek",
                                   "audio_duration_ms": 3000, "latency": 250, "is_final": True})

    async def close(self):
        pass


class FakeTTS:
    instances: list = []

    def __init__(self, cfg, key):
        self.texts, self.pcm, self.closed = [], pcm_bytes(), False
        FakeTTS.instances.append(self)

    async def synthesize(self, text, on_chunk, on_first_audio=None):
        self.texts.append(text)
        await on_chunk(self.pcm[:4000])
        if on_first_audio:
            on_first_audio()
        await on_chunk(self.pcm[4000:])
        return self.pcm, 0.05, 1

    async def aclose(self):
        self.closed = True


def make_cfg(tmp_path, **kw):
    return replace(VoiceConfig(), calls_dir=tmp_path / "calls", audio_dir=tmp_path / "audio", ledger_path=tmp_path / "credits.json",
                   lock_path=tmp_path / "lock", tts_log_path=tmp_path / "tts.jsonl", tts_first_429_path=tmp_path / "f429",
                   tick_s=0.05, unmute_holdoff_s=0.05, **kw)


def live_client(tmp_path, **kw):
    TalkingLink.instances.clear()
    FakeTTS.instances.clear()
    cfg = make_cfg(tmp_path)
    app = create_app(Gate(), cfg, key_loader=lambda: KEY, link_factory=TalkingLink, tts_client_factory=FakeTTS, live=True, **kw)
    return TestClient(app), cfg, app


def mic(i):
    t = i * 0.02 + np.arange(960) / 48000
    return (np.sin(2 * math.pi * 440 * t) * 0.5 * 32767).astype("<i2").tobytes()


def test_a_full_live_call_through_the_web_socket(tmp_path):
    client, cfg, app = live_client(tmp_path)
    got, audio = [], bytearray()
    with client.websocket_connect("/ws/call", headers=ORIGIN) as ws:
        ws.send_json({"type": "start", "sample_rate": 48000})
        stop = threading.Event()

        def feed():                                              # the browser's microphone, one chunk every 20 ms
            i = 0
            while not stop.is_set():
                ws.send_bytes(mic(i))
                i += 1
                time.sleep(0.02)

        feeder = threading.Thread(target=feed)
        feeder.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            message = ws.receive()
            if message.get("bytes") is not None:
                audio += message["bytes"]
                continue
            data = json.loads(message["text"])
            got.append(data)
            if data["type"] == "uncle_speech_end":               # the page plays the audio and says so
                ws.send_json({"type": "playing", "turn": data["turn"], "output_latency_ms": 20})
                ws.send_json({"type": "playback_done", "turn": data["turn"], "underruns": 0, "audio_state": "running"})
            if data["type"] == "latency":
                break
        stop.set()
        feeder.join()
        ws.send_json({"type": "hangup"})
        while got[-1]["type"] != "ended":
            message = ws.receive()
            if message.get("text"):
                got.append(json.loads(message["text"]))
    kinds = [g["type"] for g in got]
    assert kinds.index("turn") < kinds.index("uncle_speech_start") < kinds.index("uncle_speech_end") < kinds.index("latency")
    turn = next(g for g in got if g["type"] == "turn")
    assert [f["value"] for f in turn["intel"]] == ["47291"] and turn["intel"][0]["quote"] and "stage_timeline" in turn
    assert bytes(audio) == FakeTTS.instances[0].pcm and FakeTTS.instances[0].texts == [turn["uncle_text"]]
    latency = next(g for g in got if g["type"] == "latency")
    assert latency["wait_s"] >= 0.3 and latency["page_playing_s"] is not None and latency["underruns"] == 0
    assert got[-1]["type"] == "ended" and got[-1]["reason"] == "hangup"
    folder = next(cfg.calls_dir.iterdir())
    assert (folder / "turns.jsonl").exists() and (folder / "latency.jsonl").exists() and (folder / "findings.json").exists()


def test_the_server_command_starts_the_live_loop_and_the_default_app_is_listening_only(monkeypatch, tmp_path):
    seen = {}
    real = server_module.create_app

    def spy(gate, *a, **kw):
        seen["live"] = kw.get("live")
        return real(gate, *a, **kw)

    monkeypatch.setattr(server_module, "create_app", spy)
    monkeypatch.setattr(server_module, "VoiceConfig", lambda: make_cfg(tmp_path))
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    server_module.main([])
    assert seen["live"] is True
    app = create_app(Gate(), make_cfg(tmp_path))                                  # create_app's own default stays stop 1
    assert app.state.audio_cache is not None


def test_the_voice_test_and_the_calls_share_one_in_memory_audio_cache(tmp_path):
    client, cfg, app = live_client(tmp_path)
    with client.websocket_connect("/ws/speak", headers=ORIGIN) as ws:
        ws.send_json({"type": "speak"})
        while json.loads(ws.receive().get("text") or '{"type": "bytes"}')["type"] not in ("speech_end", "speech_error"):
            pass
    assert len(app.state.audio_cache) == 1


# ------------------------------------------------------------------ the page's own code, run in Node
NODE_STUB = r"""
const fs = require("fs"), vm = require("vm");
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ""; this._text = ""; this.style = {}; this.dataset = {};
                     this.disabled = false; this.scrollTop = 0; this.scrollHeight = 0; }
  appendChild(c) { this.children.push(c); return c; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  addEventListener() {}
}
const store = {};
const byId = (id) => store[id] || (store[id] = new El(id));
const sent = [];
const ctxStub = vm.createContext({
  document: { getElementById: byId, createElement: (t) => new El(t), createTextNode: (t) => ({ textContent: String(t) }) },
  window: { addEventListener() {}, isSecureContext: true }, navigator: {}, fetch: () => Promise.reject(new Error("no network")),
  setTimeout: (f, ms) => { return 0; }, location: { host: "x" }, WebSocket: { OPEN: 1 }, AudioContext: function () {},
  DataView, Uint8Array, Int16Array, console, sent,
});
vm.runInContext(fs.readFileSync("mirage/web/app.js", "utf8"), ctxStub);
const run = (code) => vm.runInContext(code, ctxStub);
const send = (m) => run("onServerMessage(" + JSON.stringify(m) + ")");
const text = (id) => store[id].textContent;
"""


def node(script: str) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", NODE_STUB + script], capture_output=True, encoding="utf-8", cwd=ROOT, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


TURN = {"type": "turn", "turn": 1, "heard": "main CBI se bol raha hoon case number hai chaar saat do nau ek, mera phone number hai bahut lamba",
        "stage_from": "S0", "stage": "S1", "stage_reasons": ["authority word: CBI"], "scenario": "digital_arrest", "signals": ["ask_install"],
        "tactic": "app_not_opening", "gate_action": None, "uncle_text": "Woh app nahin khul raha beta.",
        "wait": {"wait_s": 0.42, "base_wait_s": 0.3, "digit_mode": False, "hold_extra_s": 0.12},
        "timing": {"brain_ms": 3.5, "gate_ms": 1.5, "caller_end_est_s": 4.0, "last_transcript_s": 5.0},
        "intel": [{"kind": "case", "value": "47291", "confidence": "keyword", "valid": True, "reason": "", "needs_confirmation": False,
                   "quote": "chaar saat do nau ek", "t": 61.0, "suggested": None},
                  {"kind": "ambiguous", "value": "<b>12</b>", "confidence": "none", "valid": False, "reason": "too short", "needs_confirmation": True,
                   "quote": "x", "t": 3.0, "suggested": None}],
        "stage_timeline": [{"t": 33.0, "turn": 1, "from": "S0", "to": "S1"}]}


def test_the_page_shows_stage_intel_cards_with_confidence_and_quotes_and_a_latency_row():
    r = node("""
      send(%s);
      send({type: "latency", turn: 1, cached: false, tts_first_byte_ms: 340, arrival_to_audible_s: 1.234});
      const row = store.latencyBody.children[0].children.map((c) => c.textContent);
      console.log(JSON.stringify({stage: text("stageNow"), scenario: text("stageScenario"), timeline: text("stageTimeline"),
        intel: text("intelCards"), row}));
    """ % json.dumps(TURN))
    assert r["stage"] == "S1" and "digital_arrest" in r["scenario"] and "S0 → S1 at 0:33" in r["timeline"]
    assert "Case number" in r["intel"] and "47291" in r["intel"] and "confidence: keyword" in r["intel"] and "valid format" in r["intel"]
    assert "“chaar saat do nau ek” at 1:01" in r["intel"]
    assert "<b>12</b>" in r["intel"] and "needs confirmation" in r["intel"] and "not valid: too short" in r["intel"]      # shown as text
    assert r["row"][0] == "1" and r["row"][2] == "0.42" and r["row"][3] == "5" and r["row"][4] == "340" and r["row"][5] == "1.23"
    assert "held 0.1 s" in r["row"][6] and "app_not_opening" in r["row"][6]
    assert "…" in r["row"][1]                                                           # a long line is shortened


def test_the_page_says_when_to_wait_and_when_it_is_your_turn():
    r = node("""
      send({type: "mic", muted: true}); const a = [text("micState"), store.micState.style.display];
      send({type: "mic", muted: false}); const b = [text("micState"), store.micState.style.display];
      console.log(JSON.stringify({a, b}));
    """)
    assert "Uncle is speaking" in r["a"][0] and "lost" in r["a"][0] and r["a"][1] == "block"
    assert "Your turn" in r["b"][0]


def test_notices_stay_on_the_page_in_plain_words():
    r = node("""
      send({type: "notice", message: "Prisma reported speech while Uncle's line was playing."});
      send({type: "notice", message: "The page did not report that Uncle's line finished."});
      console.log(JSON.stringify({list: text("noticeList"), status: text("status")}));
    """)
    assert "while Uncle's line was playing" in r["list"] and "did not report" in r["list"] and "did not report" in r["status"]


def test_a_blocked_browser_audio_state_is_reported_to_the_user_and_the_server():
    r = node("""
      run("ws = {readyState: 1, send(m) { sent.push(JSON.parse(m)); }}; ctx = {state: 'suspended', resume() {}, sampleRate: 48000, currentTime: 0};");
      send({type: "uncle_speech_start", turn: 1, text: "Haan ji boliye", sample_rate: 24000});
      console.log(JSON.stringify({notices: text("noticeList"), sent, conv: text("transcript")}));
    """)
    assert "not letting this page play sound" in r["notices"]
    assert r["sent"][0]["type"] == "page_warning" and r["sent"][0]["code"] == "audio_suspended"
    assert "Uncle: Haan ji boliye" in r["conv"]


def test_playing_and_playback_done_are_sent_to_the_server_by_the_pages_player():
    r = node("""
      const timers = [];
      ctxStub.setTimeout = (f, ms) => { timers.push(f); return 0; };
      run("ws = {readyState: 1, send(m) { sent.push(JSON.parse(m)); }};");
      const audio = { currentTime: 5, state: "running", sampleRate: 48000, outputLatency: 0.02, sources: [],
        createBuffer(c, n, rate) { const d = new Float32Array(n); return { duration: n / rate, getChannelData: () => d }; },
        createBufferSource() { const s = { start(t) { s.at = t; }, connect() {} }; audio.sources.push(s); return s; }, destination: {} };
      ctxStub.audio = audio;
      run("ctx = audio;");
      send({type: "uncle_speech_start", turn: 3, text: "ji", sample_rate: 24000});
      run("callPlayer.push(new Uint8Array(480).buffer);");
      send({type: "uncle_speech_end", turn: 3, audio_s: 0.01});
      timers.forEach((f) => f());                                  // the "first sound starts" timer fires
      audio.sources.forEach((s) => s.onended());                   // the audio finishes
      console.log(JSON.stringify({sent}));
    """)
    kinds = [m["type"] for m in r["sent"]]
    assert kinds == ["playing", "playback_done"]
    assert r["sent"][0]["turn"] == 3 and r["sent"][0]["output_latency_ms"] == 20
    assert r["sent"][1] == {"type": "playback_done", "turn": 3, "underruns": 0, "audio_state": "running"}


def test_the_page_builds_everything_from_text_and_never_from_html():
    source = (ROOT / "mirage" / "web" / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in source and "outerHTML" not in source and "insertAdjacentHTML" not in source and "document.write" not in source
    html = (ROOT / "mirage" / "web" / "index.html").read_text(encoding="utf-8")
    for needed in ("STAGED DEMO: synthetic data", "Hang up", "Check audio", "Wait for Uncle to finish", "latencyBody", "intelCards"):
        assert needed in html
