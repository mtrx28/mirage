"""Latency test: how long after each sentence ends does Prisma v3.0 return its transcript?

Plays data/smoke/latency.wav (7 short sentences, ~1 s pauses, 8 kHz mono) to the realtime
STT endpoint once per run and prints, per sentence, the time from the end of the speech to the
transcript. Run it once per --variant to compare settings:

  baseline    x-min-silence-ms=500 (the docs' default)
  lowsilence  x-min-silence-ms=300 (lower = faster cut, but may split a sentence at a short pause)
  commit      baseline + we send {"type": "commit"} shortly after each sentence ends,
              imitating a client-side detector that spots the end of speech itself
  maxspeech   x-max-speech-s=6, meant for a long unbroken clip (e.g. clip.wav), to see forced cuts

Names used, all from docs.gnani.ai/api/STT/stt-websocket.md:
  header  x-min-silence-ms   default 500, range 100-2000
  header  x-max-speech-s     float 5.0-120.0, off by default
  message {"type": "commit"} closes the open utterance immediately
Partial transcripts are NOT documented, so nothing here tries to enable them.

Budget: every run adds a worst-case cost (ALL streamed seconds at 0.45 credits/min, silence
included, although silence looked free) to data/smoke/latency_spend.json. A run that would push
the running total past 2 credits is refused.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from array import array
from datetime import datetime
from pathlib import Path

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from _common import ROOT, SMOKE_DIR, confirm, load_gnani_key, log_run, scrub
from smoke_prisma import FRAME_BYTES, FRAME_SECONDS, SAMPLE_RATE, URL, load_clip

CREDITS_PER_MIN = 0.45          # measured on day 1 (~0.45/min of detected speech)
SPEND_CAP = 2.0                 # total credits this script may ever spend
LEDGER = SMOKE_DIR / "latency_spend.json"
TAIL_SILENCE_S = 2.0

SENTENCES = [
    "Namaste, main CBI officer bol raha hoon.",
    "Aapke naam se ek parcel pakda gaya hai.",
    "Aapko abhi case number note karna hoga.",
    "Case number hai chaar saat do nau ek.",
    "Ab payment ke liye account number likhiye.",
    "Account number hai ek do teen chaar paanch chhe saat aath.",
    "Jaldi kijiye, time kam hai.",
]

# variant -> extra upgrade headers
VARIANTS = {
    "baseline": {"x-min-silence-ms": "500"},
    "lowsilence": {"x-min-silence-ms": "300"},
    "commit": {"x-min-silence-ms": "500"},
    "maxspeech": {"x-min-silence-ms": "500", "x-max-speech-s": "6"},
}


def find_segments(pcm: bytes, min_gap_s: float = 0.6, min_len_s: float = 0.3) -> list[tuple[float, float]]:
    """Find where speech starts and ends using loudness per 20 ms window (no libraries needed).

    Gaps shorter than min_gap_s are treated as a pause inside one sentence.
    """
    samples = array("h")
    samples.frombytes(pcm)
    win = SAMPLE_RATE // 50
    rms = []
    for i in range(0, len(samples) - win + 1, win):
        chunk = samples[i:i + win]
        rms.append((sum(s * s for s in chunk) / win) ** 0.5)
    if not rms:
        return []
    ordered = sorted(rms)
    noise, loud = ordered[len(ordered) // 5], ordered[int(len(ordered) * 0.95)]
    threshold = max(noise * 3, loud * 0.08, 50)
    raw, start = [], None
    for i, level in enumerate(rms):
        if level > threshold and start is None:
            start = i
        elif level <= threshold and start is not None:
            raw.append([start, i])
            start = None
    if start is not None:
        raw.append([start, len(rms)])
    merged: list[list[int]] = []
    for seg in raw:
        if merged and (seg[0] - merged[-1][1]) * 0.02 < min_gap_s:
            merged[-1][1] = seg[1]
        else:
            merged.append(seg)
    return [(a * 0.02, b * 0.02) for a, b in merged if (b - a) * 0.02 >= min_len_s]


def load_spent() -> float:
    try:
        return float(json.loads(LEDGER.read_text())["estimated_credits"])
    except (OSError, ValueError, KeyError):
        return 0.0


def save_spent(total: float) -> None:
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps({"estimated_credits": round(total, 3)}))


async def stream(ws, pcm: bytes, commit_at: list[float], events: list[dict], t0: float) -> None:
    """Send paced frames; before the frame at stream time T, send any commit due by then."""
    pending = sorted(commit_at)
    next_t = time.monotonic()
    for i in range(0, len(pcm), FRAME_BYTES):
        stream_time = (i // FRAME_BYTES) * FRAME_SECONDS
        while pending and pending[0] <= stream_time:
            await ws.send(json.dumps({"type": "commit"}))
            events.append({"t": round(time.monotonic() - t0, 3), "type": "commit_sent", "planned": pending.pop(0)})
        await ws.send(pcm[i:i + FRAME_BYTES])
        next_t += FRAME_SECONDS
        delay = next_t - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


async def run(args, key: str, pcm: bytes, segments: list[tuple[float, float]]) -> list[dict]:
    headers = {
        "x-api-key-id": key,
        "model": "prisma-v3.0",
        "language_code": args.language,
        "format": args.format,
        "x-sample-rate": str(SAMPLE_RATE),
        "x-vad-threshold": "0.7",
        "x-min-speech-ms": "200",
        "x-idle-timeout-sec": "300",
        **VARIANTS[args.variant],
    }
    if args.silence_ms:
        headers["x-min-silence-ms"] = str(args.silence_ms)
    commit_at = []
    if args.variant == "commit":
        commit_at = [end + args.commit_after_ms / 1000 for _, end in segments]

    events: list[dict] = []
    async with websockets.connect(URL, additional_headers=headers, ping_interval=None, open_timeout=15) as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if first.get("type") != "connected":
            sys.exit("Expected a 'connected' message first; stopping.")
        t0 = time.monotonic()
        events.append({"t": 0.0, **first})
        print("connected, config echoed by server:", json.dumps(first.get("config"), ensure_ascii=False))

        async def receive() -> None:
            async for raw in ws:
                msg = json.loads(raw)
                events.append({"t": round(time.monotonic() - t0, 3), **msg})
                if msg.get("type") in ("transcript", "error"):
                    print(f"  [{events[-1]['t']:6.2f}s] {msg.get('type')}: {scrub(msg.get('text') or msg.get('message'))!r}")

        receiver = asyncio.create_task(receive())
        await stream(ws, pcm, commit_at, events, t0)
        print("all audio sent; waiting for the last transcript...")
        deadline, seen, quiet_since = time.monotonic() + 15, len(events), time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            if len(events) != seen:
                seen, quiet_since = len(events), time.monotonic()
            elif time.monotonic() - quiet_since > 3:
                break
        await ws.close()
        try:
            await receiver
        except ConnectionClosed:
            pass
    return events


def report(events: list[dict], segments: list[tuple[float, float]]) -> list[float]:
    transcripts = [e for e in events if e.get("type") == "transcript"]
    print(f"\nspeech segments found in the WAV: {len(segments)} | transcripts received: {len(transcripts)}")
    if len(segments) != len(transcripts):
        print("  counts differ, so rows are matched in order and may be off by one; read them with care.")
    deltas = []
    print(f"{'#':>2} {'speech ended':>12} {'transcript at':>13} {'delay':>7} {'server ms':>9}  text")
    for i, tr in enumerate(transcripts):
        end = segments[i][1] if i < len(segments) else None
        delay = tr["t"] - end if end is not None else None
        if delay is not None:
            deltas.append(delay)
        end_txt = f"{end:9.2f} s" if end is not None else "         ?"
        delay_txt = f"{delay:5.2f} s" if delay is not None else "      ?"
        print(f"{i + 1:>2} {end_txt:>12} {tr['t']:11.2f} s {delay_txt:>7} {str(tr.get('latency')):>9}  {tr.get('text')!r}")
    if deltas:
        print(f"\ndelay from end of speech to transcript: median {statistics.median(deltas):.2f} s, "
              f"mean {statistics.mean(deltas):.2f} s, worst {max(deltas):.2f} s")
    return deltas


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--variant", required=True, choices=list(VARIANTS))
    p.add_argument("--wav", type=Path, default=SMOKE_DIR / "latency.wav", help="mono 16-bit 8000 Hz WAV")
    p.add_argument("--silence-ms", type=int, default=0, help="override x-min-silence-ms (100-2000)")
    p.add_argument("--commit-after-ms", type=int, default=200,
                   help="commit variant: how long after the speech ends the commit is sent")
    p.add_argument("--language", default="hi-IN")
    p.add_argument("--format", default="codemix", choices=["verbatim", "transcribe", "codemix", "transliterate"])
    p.add_argument("--yes", action="store_true", help="skip the confirmation question")
    p.add_argument("--dry-run", action="store_true", help="analyse the WAV and print the plan; no network, no key needed")
    args = p.parse_args()

    clip = load_clip(args.wav)
    pcm = clip + bytes(int(TAIL_SILENCE_S * SAMPLE_RATE) * 2)
    pcm += bytes(-len(pcm) % FRAME_BYTES)
    total_s = len(pcm) / SAMPLE_RATE / 2
    segments = find_segments(clip)
    est = total_s / 60 * CREDITS_PER_MIN
    spent = load_spent()

    print(f"Plan: variant={args.variant}, stream {total_s:.1f} s (takes {total_s:.0f} s of real time)")
    print(f"      headers that differ from the default: { {k: v for k, v in VARIANTS[args.variant].items()} }"
          + (f" (silence overridden to {args.silence_ms})" if args.silence_ms else ""))
    print(f"      worst-case cost {est:.2f} credits; spent by this script so far {spent:.2f} of {SPEND_CAP:.1f}")
    print(f"Speech segments I can see in the WAV ({len(segments)}; expecting {len(SENTENCES)} for latency.wav):")
    for i, (a, b) in enumerate(segments, 1):
        print(f"  {i}: {a:6.2f} s -> {b:6.2f} s")
    if args.wav.name == "latency.wav" and len(segments) != len(SENTENCES):
        print("  WARNING: not 7 segments. Pauses may be too short or too noisy; delays below will be unreliable.")
    if spent + est > SPEND_CAP:
        sys.exit(f"Budget guard: this run would take the script past {SPEND_CAP:.1f} credits. Stopping.")
    if args.dry_run:
        print("Dry run: nothing was sent.")
        return

    key = load_gnani_key()
    if not confirm("Start streaming?", args.yes):
        sys.exit("Cancelled.")
    save_spent(spent + est)  # recorded before the run, so a crash cannot hide the cost
    wall = time.monotonic()
    try:
        events = asyncio.run(run(args, key, pcm, segments))
    except InvalidStatus as e:
        sys.exit(f"Server refused the connection: HTTP {e.response.status_code}.")
    except (ConnectionClosed, OSError, asyncio.TimeoutError) as e:
        sys.exit(f"Connection problem: {scrub(e)}")
    wall = time.monotonic() - wall

    deltas = report(events, segments)
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    out = SMOKE_DIR / f"latency_{args.variant}_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({"segments": segments, "events": events}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"raw events saved: {out.relative_to(ROOT)}")
    log_run(
        script="smoke_latency", label=args.variant, model="prisma-v3.0", audio_seconds=round(total_s, 1),
        speech_seconds="", characters="", first_result_s="", wall_s=round(wall, 1),
        balance_before="", balance_after="",
        note=f"median_delay={statistics.median(deltas):.2f}s" if deltas else "no transcripts",
    )


if __name__ == "__main__":
    main()
