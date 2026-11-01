"""Day-1 smoke test: get Timbre v3.0 to speak ~1,000 characters and report time-to-first-audio.

Uses the SSE endpoint (docs.gnani.ai/api/TTS/tts-sse.md). We ask for raw 16-bit PCM
and wrap it in a WAV header ourselves, so the audio length is exact.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import httpx

from _common import ROOT, SMOKE_DIR, confirm, load_gnani_key, log_run, scrub

URL = "https://api.vachana.ai/api/v1/tts/sse"
MODEL = "timbre-v3.0"
TARGET_CHARS = 1000
DEFAULT_CAP_CHARS = 5000
EST_CREDITS_PER_1000_CHARS = 2.7      # blueprint estimate; this test exists to replace it

# Rajesh Uncle, invented lines. No real names, numbers or accounts.
EN = [
    "Hello, who is speaking please? The line is not very clear.",
    "Oh, you are calling from the bank? Let me find my glasses first.",
    "I am a retired clerk, and I do not understand all these new things.",
    "Please speak slowly and repeat that number again, I will write it down.",
    "Where should I send the money? Please say the account number one digit at a time.",
    "My phone is running a little slow, and the app is not opening at all.",
    "Oh dear, is it really that serious? I am quite worried now.",
    "One minute please, let me drink some water and then we can talk calmly.",
]
HI_DEVA = [
    "हेलो, कौन बोल रहा है बेटा? आवाज़ थोड़ी कम आ रही है।",
    "अच्छा अच्छा, आप बैंक से बोल रहे हैं? मुझे ज़रा चश्मा ढूँढने दीजिए।",
    "मैं रिटायर्ड क्लर्क हूँ, मुझे ये सब नई चीज़ें ज़्यादा समझ नहीं आतीं।",
    "ज़रा धीरे बोलिए, और वो नंबर फिर से बताइए, मैं लिख लेता हूँ।",
    "पैसे कहाँ भेजने हैं? आप खाता नंबर एक-एक अंक करके बोलिए।",
    "मेरा फ़ोन थोड़ा धीमा चल रहा है, ऐप खुल ही नहीं रहा बेटा।",
    "अरे बाप रे, इतनी बड़ी बात हो गई? मैं तो घबरा गया हूँ।",
    "एक मिनट रुकिए, पानी पी लेता हूँ, फिर आराम से बात करते हैं।",
]
HI_LATIN = [
    "Hello, kaun bol raha hai beta? Awaaz thodi kam aa rahi hai.",
    "Achha achha, aap bank se bol rahe hain? Mujhe zara chashma dhoondhne dijiye.",
    "Main retired clerk hoon, mujhe ye sab nayi cheezein zyada samajh nahin aati.",
    "Zara dheere boliye, aur woh number phir se bataiye, main likh leta hoon.",
    "Paise kahan bhejne hain? Aap account number ek ek ank karke boliye.",
    "Mera phone thoda slow chal raha hai, app khul hi nahin raha beta.",
    "Arre baap re, itni badi baat ho gayi? Main toh ghabra gaya hoon.",
    "Ek minute rukiye, paani pee leta hoon, phir aaraam se baat karte hain.",
]
# Three tags, the docs' "two or three per paragraph". Sentence index -> tag, first pass only.
TAGS_AT = {1: "<coughs>", 4: "<pauses>", 7: "<sigh>"}

# case name -> (language code, sentences, with tags)
CASES = {
    "en": ("en-IN", EN, False),
    "en-tags": ("en-IN", EN, True),
    "hi-deva": ("hi-IN", HI_DEVA, True),
    "hi-latin": ("hi-en", HI_LATIN, True),
}


# One short Latin-script Hinglish line (~150 characters) for comparing voices cheaply.
VOICE_LINE = (
    "Hello, kaun bol raha hai beta? Awaaz thodi kam aa rahi hai, zara dheere boliye. "
    "Main chashma dhoondh raha hoon, ek minute rukiye."
)
VOICE_LINE_LANGUAGE = "hi-en"
# Timbre v3.0 voices to audition for Rajesh Uncle (the docs list names but no gender; judge by ear).
CANDIDATE_VOICES = "Yashraj,Prakash,Devansh,Avinash"


def build_text(sentences: list[str], tagged: bool) -> str:
    parts, total, i = [], 0, 0
    while total < TARGET_CHARS:
        s = sentences[i % len(sentences)]
        if tagged and i in TAGS_AT:
            s = f"{TAGS_AT[i]} {s}"
        parts.append(s)
        total += len(s) + 1
        i += 1
    return " ".join(parts)


def synthesize(key: str, text: str, voice: str, language: str, rate: int) -> tuple[bytes, float | None, float]:
    payload = {
        "text": text, "model": MODEL, "voice": voice, "language": language, "speed": 1.0,
        "audio_config": {
            "sample_rate": rate, "num_channels": 1, "sample_width": 2,
            "encoding": "linear_pcm", "container": "raw",
        },
    }
    headers = {"X-API-Key-ID": key, "Content-Type": "application/json"}
    t0, first, event = time.monotonic(), None, None
    audio = bytearray()
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
        with client.stream("POST", URL, headers=headers, json=payload) as r:
            if r.status_code != 200:
                body = r.read().decode("utf-8", "replace")
                sys.exit(f"HTTP {r.status_code}: {scrub(body)[:500]}")
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = json.loads(line[5:])
                    if event == "chunk" and data.get("audio"):
                        if first is None:
                            first = time.monotonic() - t0
                        audio += base64.b64decode(data["audio"])
                    elif event == "error":
                        sys.exit(f"TTS error event: {scrub(data)}")
    return bytes(audio), first, time.monotonic() - t0


def save_audio(path: Path, audio: bytes, rate: int) -> float:
    """Write a WAV and return its length in seconds. Handles a server that sends its own header."""
    if audio[:4] == b"RIFF":
        print("note: server sent a WAV header even though raw PCM was requested; saving as-is")
        path.write_bytes(audio)
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio)
    return len(audio) / 2 / rate


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--case", default="all", choices=["all", *CASES, "voice-line"],
                   help="which sample to speak; 'voice-line' is the short ~150-character line for comparing voices")
    p.add_argument("--voice", default="Yashraj", help="Timbre v3.0 voice name (e.g. Yashraj, Devansh, Avinash)")
    p.add_argument("--voices", default="",
                   help=f"comma-separated voices to try one after another, e.g. {CANDIDATE_VOICES} (overrides --voice)")
    p.add_argument("--sample-rate", type=int, default=24000, choices=[8000, 16000, 22050, 24000, 44100, 48000])
    p.add_argument("--label", default="timbre-run", help="prefix for this run in the CSV log")
    p.add_argument("--yes", action="store_true", help="skip the confirmation question")
    p.add_argument("--dry-run", action="store_true", help="print the texts and character counts; no network, no key needed")
    p.add_argument("--allow-long", action="store_true", help="lift the 5,000-character guard")
    args = p.parse_args()

    voices = [v.strip() for v in args.voices.split(",") if v.strip()] or [args.voice]
    if args.case == "voice-line":
        names = ["voice-line"]
        texts = {"voice-line": VOICE_LINE}
        languages = {"voice-line": VOICE_LINE_LANGUAGE}
    else:
        names = list(CASES) if args.case == "all" else [args.case]
        texts = {n: build_text(CASES[n][1], CASES[n][2]) for n in names}
        languages = {n: CASES[n][0] for n in names}
    total = sum(len(t) for t in texts.values()) * len(voices)
    for n, t in texts.items():
        print(f"\n[{n}] language={languages[n]} chars={len(t)} utf8_bytes={len(t.encode('utf-8'))}\n{t}")
    print(f"\nPlan: {len(names) * len(voices)} request(s), {total} characters, voices={','.join(voices)}, {args.sample_rate} Hz")
    print(f"      blueprint estimate: ~{total / 1000 * EST_CREDITS_PER_1000_CHARS:.1f} credits (unverified)")
    if total > DEFAULT_CAP_CHARS and not args.allow_long:
        sys.exit(f"Budget guard: {total} characters exceeds {DEFAULT_CAP_CHARS}. Use fewer cases, or --allow-long.")
    if args.dry_run:
        print("Dry run: nothing was sent.")
        return

    key = load_gnani_key()
    if not confirm("Note your dashboard balance first. Send these requests?", args.yes):
        sys.exit("Cancelled.")
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    for n in names:
        language = languages[n]
        for voice in voices:
            print(f"\n== {n} / {voice} ==")
            try:
                audio, first, wall = synthesize(key, texts[n], voice, language, args.sample_rate)
            except httpx.HTTPError as e:
                sys.exit(f"Network problem: {scrub(e)}")
            out = SMOKE_DIR / f"tts_{n}_{voice}_{datetime.now():%Y%m%d_%H%M%S}.wav"
            seconds = save_audio(out, audio, args.sample_rate)
            first_txt = f"{first:.2f} s" if first is not None else "no audio chunk arrived"
            print(f"first audio chunk: {first_txt} | total request: {wall:.2f} s | audio length: {seconds:.1f} s")
            print(f"saved: {out.relative_to(ROOT)}")
            log_run(
                script="smoke_timbre", label=f"{args.label}-{n}-{voice}", model=MODEL, audio_seconds=round(seconds, 1),
                speech_seconds="", characters=len(texts[n]), first_result_s=first, wall_s=round(wall, 2),
                balance_before="", balance_after="", note=f"voice={voice} lang={language} bytes={len(texts[n].encode('utf-8'))}",
            )
            time.sleep(1)  # the TTS docs allow only one active request at a time


if __name__ == "__main__":
    main()
