"""Day-1 smoke test: stream a short 8 kHz WAV to Prisma v3.0 and print what comes back.

Protocol notes (from docs.gnani.ai/api/STT/stt-websocket.md): all settings are
sent as WebSocket upgrade headers, audio is raw 16-bit mono PCM in 1,024-byte
frames paced in real time, and there is no end-of-stream message, so we send
silence at the end and wait for the last transcript.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from _common import ROOT, SMOKE_DIR, confirm, load_gnani_key, log_run, scrub

URL = "wss://api.vachana.ai/stt/v3/stream"
SAMPLE_RATE = 8000
FRAME_BYTES = 1024                    # 512 samples x 2 bytes
FRAME_SECONDS = 512 / SAMPLE_RATE     # 0.064 s: one frame every 64 ms at 8 kHz
DEFAULT_CAP_S = 6 * 60                # budget guard
HARD_CAP_S = 15 * 60                  # Prisma's own session limit, only with --allow-long
EST_CREDITS_PER_MIN = 0.45            # blueprint estimate; this test exists to replace it

FFMPEG_HINT = (
    "Convert your recording with:\n"
    "  ffmpeg -i <your file> -ac 1 -ar 8000 -c:a pcm_s16le data\\smoke\\clip.wav"
)


def load_clip(path: Path) -> bytes:
    if not path.exists():
        sys.exit(f"{path} does not exist.\n{FFMPEG_HINT}")
    try:
        with wave.open(str(path), "rb") as w:
            fmt = (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getcomptype())
            pcm = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        sys.exit(f"Cannot read {path} as a plain PCM WAV ({e}).\n{FFMPEG_HINT}")
    if fmt != (1, 2, SAMPLE_RATE, "NONE"):
        sys.exit(
            f"{path} is {fmt[0]} channel(s), {fmt[1] * 8}-bit, {fmt[2]} Hz. "
            f"Need mono, 16-bit, {SAMPLE_RATE} Hz.\n{FFMPEG_HINT}"
        )
    return pcm


def build_stream(clip: bytes, repeat: int, silence_s: float) -> bytes:
    pcm = clip * repeat + bytes(int(silence_s * SAMPLE_RATE) * 2)
    return pcm + bytes(-len(pcm) % FRAME_BYTES)  # pad to whole frames


def show(msg: dict, now: float) -> None:
    kind = msg.get("type")
    if kind == "transcript":
        print(
            f"[{now:6.2f}s] transcript  lang={msg.get('detected_language_code')}  "
            f"server_latency={msg.get('latency')}ms  audio={msg.get('audio_duration_ms')}ms\n"
            f"            {msg.get('text')!r}"
        )
    elif kind == "error":
        print(f"[{now:6.2f}s] ERROR       {scrub(msg.get('message'))}")
    elif kind == "connected":
        print(f"[{now:6.2f}s] connected   {json.dumps(msg.get('config'), ensure_ascii=False)}")
    else:
        print(f"[{now:6.2f}s] {kind}")


async def send_audio(ws, pcm: bytes) -> float:
    """Send frames at real-time cadence, scheduled against a fixed clock so delays don't accumulate."""
    next_t = time.monotonic()
    for i in range(0, len(pcm), FRAME_BYTES):
        await ws.send(pcm[i:i + FRAME_BYTES])
        next_t += FRAME_SECONDS
        delay = next_t - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
    return time.monotonic()


async def receive(ws, t0: float, events: list[dict]) -> None:
    async for raw in ws:
        now = time.monotonic() - t0
        msg = json.loads(raw)
        events.append({"t": round(now, 3), **msg})
        show(msg, now)


async def run(args: argparse.Namespace, key: str, pcm: bytes, speech_s: float) -> None:
    headers = {
        "x-api-key-id": key,
        "model": "prisma-v3.0",
        "language_code": args.language,
        "format": args.format,
        "x-sample-rate": str(SAMPLE_RATE),
        "x-vad-threshold": "0.7",
        "x-min-speech-ms": "200",
        "x-min-silence-ms": "500",
        "x-idle-timeout-sec": "300",
    }
    if args.keyterms:
        headers["keyterms"] = args.keyterms

    events: list[dict] = []
    wall_start = time.monotonic()
    # ping_interval=None: the docs say nothing about WebSocket pings, and frames flow constantly anyway.
    async with websockets.connect(URL, additional_headers=headers, ping_interval=None, open_timeout=15) as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), 15))
        t0 = time.monotonic()
        show(first, 0.0)
        events.append({"t": 0.0, **first})
        if first.get("type") != "connected":
            sys.exit("Expected a 'connected' message first; stopping.")

        receiver = asyncio.create_task(receive(ws, t0, events))
        t_end = await send_audio(ws, pcm) - t0
        print(f"[{t_end:6.2f}s] all audio sent; waiting for the last transcript...")

        # No end-of-stream message exists: wait until the server has been quiet for 4 s (max 20 s).
        deadline = time.monotonic() + 20
        seen, quiet_since = len(events), time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            if len(events) != seen:
                seen, quiet_since = len(events), time.monotonic()
            elif time.monotonic() - quiet_since > 4:
                break
        await ws.close()
        try:
            await receiver
        except ConnectionClosed:
            pass

    wall = time.monotonic() - wall_start
    transcripts = [e for e in events if e.get("type") == "transcript"]
    print("\n--- summary ---")
    print(f"audio sent:       {len(pcm) / SAMPLE_RATE / 2:.1f} s ({speech_s:.1f} s of it is your clip)")
    print(f"wall time:        {wall:.1f} s")
    print(f"transcripts:      {len(transcripts)}")
    first_s = None
    if transcripts:
        first_s = transcripts[0]["t"]
        print(f"first transcript: {first_s:.2f} s after the stream started")
        print(f"last transcript:  {transcripts[-1]['t'] - t_end:+.2f} s relative to the last audio frame")

    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    out = SMOKE_DIR / f"prisma_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({"events": events}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"raw events saved: {out.relative_to(ROOT)}")
    log_run(
        script="smoke_prisma", label=args.label, model="prisma-v3.0",
        audio_seconds=round(len(pcm) / SAMPLE_RATE / 2, 1), speech_seconds=round(speech_s, 1),
        characters="", first_result_s=first_s, wall_s=round(wall, 1),
        balance_before="", balance_after="", note=f"format={args.format} lang={args.language}",
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--wav", type=Path, default=SMOKE_DIR / "clip.wav", help="mono 16-bit 8000 Hz WAV")
    p.add_argument("--repeat", type=int, default=1, help="play the clip this many times back to back")
    p.add_argument("--silence-seconds", type=float, default=1.5, help="silence appended after the speech")
    # The docs say a list of 1-4 codes works, but the live server answered "exactly one locale is required".
    p.add_argument("--language", default="hi-IN", help="exactly ONE code (hi-IN, en-IN, ...) or indic-auto")
    p.add_argument("--format", default="codemix", choices=["verbatim", "transcribe", "codemix", "transliterate"])
    p.add_argument("--keyterms", default="", help="comma-separated, ASCII only (e.g. CBI,UPI,AnyDesk)")
    p.add_argument("--label", default="prisma-run", help="name for this run in the CSV log")
    p.add_argument("--yes", action="store_true", help="skip the confirmation question")
    p.add_argument("--dry-run", action="store_true", help="check the file and print the plan; no network, no key needed")
    p.add_argument("--allow-long", action="store_true", help="raise the 6-minute guard to 15 minutes")
    args = p.parse_args()

    clip = load_clip(args.wav)
    pcm = build_stream(clip, args.repeat, args.silence_seconds)
    total_s = len(pcm) / SAMPLE_RATE / 2
    speech_s = len(clip) * args.repeat / SAMPLE_RATE / 2
    cap = HARD_CAP_S if args.allow_long else DEFAULT_CAP_S
    if total_s > cap:
        sys.exit(f"Budget guard: {total_s:.0f} s of audio exceeds the {cap // 60}-minute cap. "
                 "Shorten it, or pass --allow-long (max 15 minutes).")

    print(f"Plan: stream {total_s:.1f} s of 8 kHz audio ({speech_s:.1f} s speech) to {URL}")
    print(f"      model=prisma-v3.0 language={args.language} format={args.format} (takes {total_s:.0f} s of real time)")
    print(f"      blueprint estimate: ~{total_s / 60 * EST_CREDITS_PER_MIN:.2f} credits (unverified)")
    if args.dry_run:
        print("Dry run: nothing was sent.")
        return

    key = load_gnani_key()
    if not confirm("Note your dashboard balance first. Start streaming?", args.yes):
        sys.exit("Cancelled.")
    try:
        asyncio.run(run(args, key, pcm, speech_s))
    except InvalidStatus as e:
        sys.exit(f"Server refused the connection: HTTP {e.response.status_code} (401 = key missing or invalid).")
    except (ConnectionClosed, OSError, asyncio.TimeoutError) as e:
        sys.exit(f"Connection problem: {scrub(e)}")


if __name__ == "__main__":
    main()
