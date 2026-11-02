"""How does Timbre (voice Prakash, hi-en) SAY a read-back of digits? Four ways to write the same line.

Timbre's text-normalisation guide says what it does with numerals but is silent about number WORDS ("ek do teen"),
so we listen and decide FSMConfig.digit_style (and whether the read-back lines need changes):

  words       "Achha, ye likha: zero zero chaar, saat ek teen. Aage ka phir se boliye."   (digit_style "words", today's default)
  spaced      "Achha, ye likha: 0 0 4, 7 1 3. ..."                                        (digit_style "numerals")
  plain       "Achha, ye likha: 004713. ..."                                              (what a bare numeral does)
  devanagari  "Achha, ye likha: ज़ीरो ज़ीरो चार, सात एक तीन. ..."                           (digit words in Devanagari script)

Four short requests, about 240 characters, about 0.65 credits at the measured 2.7 per 1,000 characters. The script
refuses to send more than 1 credit's worth. Files are saved to data/smoke/ (gitignored); nothing else is saved
except one row per request in the credit log.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

import httpx

from _common import ROOT, SMOKE_DIR, confirm, load_gnani_key, log_run, scrub
from smoke_timbre import EST_CREDITS_PER_1000_CHARS, MODEL, save_audio, synthesize

VOICE = "Prakash"
LANGUAGE = "hi-en"
CAP_CREDITS = 1.0
TAIL = "Aage ka phir se boliye."

CASES = {
    "words": f"Achha, ye likha: zero zero chaar, saat ek teen. {TAIL}",
    "spaced": f"Achha, ye likha: 0 0 4, 7 1 3. {TAIL}",
    "plain": f"Achha, ye likha: 004713. {TAIL}",
    "devanagari": f"Achha, ye likha: ज़ीरो ज़ीरो चार, सात एक तीन. {TAIL}",
}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--sample-rate", type=int, default=24000, choices=[8000, 16000, 22050, 24000, 44100, 48000])
    p.add_argument("--yes", action="store_true", help="skip the confirmation question")
    p.add_argument("--dry-run", action="store_true", help="print the four lines and the cost estimate; nothing is sent")
    args = p.parse_args()

    total = sum(len(t) for t in CASES.values())
    estimate = total / 1000 * EST_CREDITS_PER_1000_CHARS
    for name, text in CASES.items():
        print(f"[{name}] ({len(text)} characters) {text}")
    print(f"\nPlan: {len(CASES)} requests, {total} characters, voice {VOICE}, language {LANGUAGE}, no tags, {args.sample_rate} Hz")
    print(f"      estimate: ~{estimate:.2f} credits (measured rate ~{EST_CREDITS_PER_1000_CHARS} per 1,000 characters)")
    if estimate > CAP_CREDITS:
        sys.exit(f"Budget guard: ~{estimate:.2f} credits is above the {CAP_CREDITS:g}-credit cap.")
    if args.dry_run:
        print("Dry run: nothing was sent.")
        return

    key = load_gnani_key()
    if not confirm("Note your dashboard balance first. Send these requests?", args.yes):
        sys.exit("Cancelled.")
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in CASES.items():
        print(f"\n== {name} ==")
        try:
            audio, first, wall = synthesize(key, text, VOICE, LANGUAGE, args.sample_rate)
        except httpx.HTTPError as e:
            sys.exit(f"Network problem: {scrub(e)}")
        out = SMOKE_DIR / f"tts_digits_{name}_{datetime.now():%Y%m%d_%H%M%S}.wav"
        seconds = save_audio(out, audio, args.sample_rate)
        first_txt = f"{first:.2f} s" if first is not None else "no audio chunk arrived"
        print(f"first audio chunk: {first_txt} | total request: {wall:.2f} s | audio length: {seconds:.1f} s")
        print(f"saved: {out.relative_to(ROOT)}")
        log_run(script="smoke_digits_tts", label=f"digits-{name}", model=MODEL, audio_seconds=round(seconds, 1),
                speech_seconds="", characters=len(text), first_result_s=first, wall_s=round(wall, 2),
                balance_before="", balance_after="", note=f"voice={VOICE} lang={LANGUAGE}")
        time.sleep(1)   # Timbre allows one active request at a time


if __name__ == "__main__":
    main()
