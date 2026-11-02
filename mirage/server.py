"""The web server: serves the page and carries one call at a time over a WebSocket.

    python -m mirage.server --max-minutes 3 --keyterms off

Safety by construction:
  - it refuses to start without the safety gate (GateRequired), even though stop 1 never speaks;
  - it listens on this computer only (127.0.0.1) and refuses any other address;
  - it accepts the call WebSocket only from a page it served itself (Origin check), so another website open in
    your browser cannot start calls and spend credits;
  - one call at a time, one server process at a time (a lock file; the daily LLM counter needs this too);
  - the page has no dialer: the only thing a "call" can be is a person speaking into this computer's microphone.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from mirage.calllog import purge_old_audio
from mirage.config import ROOT, VoiceConfig
from mirage.credits import CreditLimitReached, Ledger
from mirage.prisma import PrismaError, PrismaLink, load_keyterms
from mirage.gaterequired import GateRequired, require_gate  # noqa: F401  (GateRequired is imported from here by the tests)
from mirage.session import CallSession
from mirage.timbre import SpeechQueue, Speaker, TimbreClient, TimbreError, UngatedPlan
from mirage.voicetest import fixed_line_plan

log = logging.getLogger("mirage.server")

WEB_DIR = ROOT / "mirage" / "web"
STATIC_FILES = {"app.js": "text/javascript", "capture-worklet.js": "text/javascript"}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
NO_STORE = {"Cache-Control": "no-store"}


def load_key_from_env() -> str:
    """GNANI_API_KEY from .env. The value is never printed or sent anywhere except to Prisma."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    return os.environ.get("GNANI_API_KEY", "").strip()


class ProcessLock:
    """Only one server process may run (they would share the credit ledger and the daily LLM counter unsafely).
    The operating system drops the lock when the process dies, so there is no stale lock file to clean up."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._file = handle
        return True

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def allowed_origins(cfg: VoiceConfig) -> set[str]:
    return {f"http://127.0.0.1:{cfg.port}", f"http://localhost:{cfg.port}", f"http://[::1]:{cfg.port}"}


def create_app(gate: object, cfg: VoiceConfig | None = None, *, ledger: Ledger | None = None,
               key_loader=load_key_from_env, keyterms: list[str] | None = None,
               link_factory=PrismaLink, max_call_s: float | None = None, tts_client_factory=TimbreClient) -> FastAPI:
    require_gate(gate)                                      # no gate, no server
    cfg = cfg or VoiceConfig()
    ledger = ledger or Ledger(cfg)
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if app.state.tts_client is not None:
            await app.state.tts_client.aclose()

    app = FastAPI(title="Mirage", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.active = False
    app.state.speaker = None            # built on first use, then kept so its in-memory audio cache survives between presses
    app.state.tts_client = None
    app.state.ledger = ledger
    origins = allowed_origins(cfg)

    @app.get("/")
    async def index() -> Response:
        return FileResponse(WEB_DIR / "index.html", media_type="text/html", headers=NO_STORE)

    @app.get("/static/{name}")
    async def static_file(name: str) -> Response:
        if name not in STATIC_FILES:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(WEB_DIR / name, media_type=STATIC_FILES[name], headers=NO_STORE)

    @app.get("/api/status")
    async def status() -> dict:
        return {"ledger_spent": round(ledger.spent, 2), "pool": cfg.pool_credits, "hard_stop": ledger.limit,
                "blocked": ledger.blocked(), "warning": ledger.warning(), "busy": app.state.active,
                "max_call_s": cfg.max_call_s if max_call_s is None else max_call_s}

    @app.websocket("/ws/call")
    async def call(ws: WebSocket) -> None:
        if ws.headers.get("origin") not in origins:
            await ws.close(code=1008)                       # not from our own page
            return
        await ws.accept()

        async def notify(message: dict) -> None:
            try:
                await ws.send_json(message)
            except Exception:                               # the page went away; the session will notice
                pass

        if app.state.active:
            await notify({"type": "error", "message": "A call is already running. Hang up first."})
            await ws.close()
            return
        app.state.active = True
        session: CallSession | None = None
        watcher: asyncio.Task | None = None
        reason = "browser_closed"                           # unless the page says "hangup" first
        try:
            while True:
                try:
                    message = await ws.receive()
                except RuntimeError:                        # we closed the socket ourselves (the call ended)
                    break
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    if session is not None:
                        session.feed(message["bytes"])
                    continue
                try:
                    data = json.loads(message.get("text") or "")
                except ValueError:
                    continue
                kind = data.get("type")
                if kind == "start" and session is None:
                    key = key_loader()
                    if not key:
                        await notify({"type": "error", "message": "GNANI_API_KEY is missing. Put it in the .env file "
                                      "(see .env.example) and restart the server."})
                        break
                    session = CallSession(cfg, gate, ledger, notify, key=key, keyterms=keyterms,
                                          link_factory=link_factory, max_call_s=max_call_s)
                    try:
                        await session.start(int(data.get("sample_rate") or 0))
                    except (PrismaError, CreditLimitReached) as e:
                        await notify({"type": "error", "message": str(e)})
                        break
                    watcher = asyncio.create_task(_close_when_done(session, ws))
                elif kind == "hangup":
                    reason = "hangup"
                    break
        finally:
            if session is not None:
                await session.hangup(reason)                # does nothing if the call already ended on its own
            if watcher is not None:
                watcher.cancel()
            app.state.active = False
            try:
                await ws.close()
            except Exception:
                pass

    def get_speaker() -> Speaker | None:
        if app.state.speaker is None:
            key = key_loader()
            if not key:
                return None
            app.state.tts_client = tts_client_factory(cfg, key)
            app.state.speaker = Speaker(gate, SpeechQueue(app.state.tts_client, cfg, ledger))
        return app.state.speaker

    def log_speech(record: dict) -> None:
        try:
            cfg.tts_log_path.parent.mkdir(parents=True, exist_ok=True)
            with cfg.tts_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass

    async def speak_fixed_line(ws: WebSocket) -> None:
        speaker = get_speaker()
        if speaker is None:
            await ws.send_json({"type": "speech_error", "message": "GNANI_API_KEY is missing. Put it in the .env file "
                                "(see .env.example) and restart the server."})
            return
        plan = fixed_line_plan(gate, cfg)               # the browser never supplies the text
        spent_before = ledger.spent
        try:
            await ws.send_json({"type": "speech_start", "text": plan.text, "sample_rate": cfg.tts_sample_rate,
                                "chars": len(plan.text)})

            async def forward(piece: bytes) -> None:
                await ws.send_bytes(piece)

            result = await speaker.say(plan, forward)
        except (TimbreError, UngatedPlan, CreditLimitReached) as e:
            log_speech({"ok": False, "error": str(e), "chars": len(plan.text)})
            await ws.send_json({"type": "speech_error", "message": str(e)})
            return
        credits = round(ledger.spent - spent_before, 4)
        log_speech({"ok": True, "chars": result.chars, "cached": result.cached, "first_byte_s": result.first_byte_s,
                    "total_s": round(result.total_s, 3), "attempts": result.attempts, "audio_s": round(result.seconds, 2),
                    "credits_estimate": credits})
        await ws.send_json({"type": "speech_end", "cached": result.cached, "chars": result.chars, "credits": credits,
                            "first_byte_s": None if result.first_byte_s is None else round(result.first_byte_s, 2),
                            "total_s": round(result.total_s, 2), "audio_s": round(result.seconds, 2),
                            "attempts": result.attempts, "ledger_spent": round(ledger.spent, 2)})

    @app.websocket("/ws/speak")
    async def speak(ws: WebSocket) -> None:
        if ws.headers.get("origin") not in origins:
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            while True:
                try:
                    message = await ws.receive()
                except RuntimeError:
                    break
                if message["type"] == "websocket.disconnect":
                    break
                try:
                    data = json.loads(message.get("text") or "")
                except ValueError:
                    continue
                if data.get("type") == "speak":
                    if app.state.active:                                 # a call, or another test line, is running
                        await ws.send_json({"type": "speech_error", "message": "A call or another test line is running. Wait or hang up first."})
                        continue
                    app.state.active = True                              # held only while this one line is spoken
                    try:
                        await speak_fixed_line(ws)
                    except (RuntimeError, WebSocketDisconnect):          # the page went away mid-line
                        break
                    finally:
                        app.state.active = False
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    return app


async def _close_when_done(session: CallSession, ws: WebSocket) -> None:
    """When the call ends on its own (a limit, or Prisma going away), close the page's connection."""
    await session.done.wait()
    try:
        await ws.close()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = VoiceConfig()
    p = argparse.ArgumentParser(description="Run the Mirage voice demo (stop 1: microphone to live transcript).")
    p.add_argument("--host", default=cfg.host, help="must be this computer only (127.0.0.1)")
    p.add_argument("--port", type=int, default=cfg.port)
    p.add_argument("--max-minutes", type=float, default=cfg.max_call_s / 60, help="end every call after this long")
    p.add_argument("--keyterms", choices=["on", "off"], default="on" if cfg.use_keyterms else "off",
                   help="send the keyterm list to Prisma (the docs do not say what it costs)")
    args = p.parse_args(argv)
    if args.host not in LOOPBACK:
        sys.exit(f"Refusing to listen on {args.host}. The page can spend credits, so it only listens on this computer "
                 "(127.0.0.1).")

    from dataclasses import replace
    from mirage.safety import Gate
    cfg = replace(cfg, host=args.host, port=args.port, use_keyterms=args.keyterms == "on")
    lock = ProcessLock(cfg.lock_path)
    if not lock.acquire():
        sys.exit("Another Mirage server is already running (one process only: they would share the credit ledger and "
                 "the daily counter). Close it first.")
    try:
        for name in purge_old_audio(cfg):
            print(f"Deleted saved audio older than {cfg.audio_keep_days} days: {name}")
        ledger = Ledger(cfg)
        terms = load_keyterms() if cfg.use_keyterms else None
        app = create_app(Gate(), cfg, ledger=ledger, keyterms=terms, max_call_s=args.max_minutes * 60)
        print(f"Ledger: {ledger.spent:.2f} credits spent; hard stop at {ledger.limit:.0f}. "
              f"Keyterms: {'on (' + str(len(terms)) + ' terms)' if terms else 'off'}. "
              f"Each call ends after {args.max_minutes:g} minutes.")
        if ledger.blocked():
            print("WARNING: the ledger is at or over the hard stop; calls will be refused until you run "
                  "'python -m mirage.credits sync <dashboard figure>'.")
        print(f"Open http://{cfg.host}:{cfg.port} in Chrome. Ctrl+C stops the server.")
        import uvicorn
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")
    finally:
        lock.release()


if __name__ == "__main__":
    main()
