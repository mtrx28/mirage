# Voice demo: how to run it (stops 1 to 3)

One server process, one call at a time, on this computer only.

Install (once):

    python -m pip install -r requirements.txt

Start (keyterms are off by default; there is no evidence yet that they help):

    python -m mirage.server --max-minutes 3

Then open http://127.0.0.1:8000 in Chrome (exactly that address: the browser only gives a page the microphone on
127.0.0.1 or localhost). Stop the server with Ctrl+C. `--max-minutes` is how long a call may last (the default is 14, because
Prisma cuts a stream at 15 minutes).

## What the page does

- **Start call**: the caller speaks into the microphone; Uncle answers (Rajesh Uncle, voice Prakash). He speaks only after the
  caller's first words.
- **Check audio (free)**: plays a short quiet tone while listening to the microphone and counts how many microphone chunks
  kept arriving during the tone. Run it before a session: it tells you whether this browser can capture and play at the same
  time, whether sound output is blocked, and how loud the microphone is. No server, no credits.
- **Speak the test line**: one fixed line in Uncle's voice (the server picks it). The first press costs a little; pressing again
  plays it from memory for free. Not available during a call.
- **Dashboard** (while a call runs): the conversation (the caller's transcripts and Uncle's lines), the stage and its timeline,
  intel cards (kind, value as heard, confidence, valid or not, needs confirmation, the exact quote and when), minutes held,
  credits for this call and the ledger, notices in plain words, and a table of turns with the wait, brain and gate time,
  Timbre first byte and "heard to audible" time.

## Important: half-duplex, and what the caller must do

**While Uncle's line plays, the microphone audio sent to the speech service is replaced by silence** (so his own voice cannot
be heard as the caller). **Anything the caller says over Uncle is lost.** The page shows "Uncle is speaking. Stay quiet" while
the mute is on and "Your turn" when it ends. **The caller (your friend) must wait for Uncle to finish before speaking.**
This is a known limitation of the first version. A real scammer who talks over Uncle will be partly lost.

- **A good microphone matters most.** A headphone microphone gave thin audio and lost words; a proper microphone fixed it.
  Use a good microphone for the caller, and headphones only for Uncle's voice, so his voice does not feed back.
- **Long speech arrives late.** Prisma sends a transcript only when the caller pauses, so a long stretch arrives as one late
  transcript; Uncle waits for the caller to finish (up to a limit) before he answers.

## Limits

- **Time:** at the time limit Uncle says one in-persona goodbye and the call ends. The goodbye is allowed to go past the credit
  hard stop once; if the whole credit pool is spent, or the voice is down, the call ends silently.
- **Credits:** the hard stop is 80% of the pool; a per-call cap also ends the call (with the same one goodbye).
- **Voice failures:** a line that could not be spoken is logged as never heard (the brain still thinks it said it); three in a
  row end the call.

## Data, credits, processes

- **Single process only.** A lock file stops a second server (they would share the credit ledger and the daily LLM counter
  unsafely).
- **Credits.** `python -m mirage.credits` shows the ledger; `python -m mirage.credits sync <dashboard figure> --requests <n>`
  corrects it. The ledger is an estimate and leans high. Every spoken line is also logged to `data/tts_log.jsonl`.
- **Per-call evidence** in `data/calls/<id>/`: `summary.json`, `events.jsonl`, `transcripts.jsonl` (with the raw Prisma message),
  `turns.jsonl` (what was heard, stage, findings, gate decision, Uncle's line, whether it was really spoken), `latency.jsonl`,
  `findings.json`. Raw audio is NOT saved unless `MIRAGE_SAVE_AUDIO=1` is set; saved audio goes to `data/audio/` and is deleted after
  7 days at server start. `data/` is gitignored. Transcripts contain what the caller said, so tell your friend they are kept.
- **Staged demo.** The page carries a "STAGED DEMO: synthetic data" banner. All data is invented.
- **Replaying a call offline** (no network, no audio): `python -m mirage.replay data/calls/<id>`.
