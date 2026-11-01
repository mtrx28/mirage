# Engineering log

Plain-words notes on what we measured and decided. Newest section last. Everything here was
measured on 1 Nov 2026 with a synthetic test clip (an invented phone number and account number, my own
voice). Nothing here is a benchmark unless it says so.

## Day 1: smoke tests (1 Nov 2026)

Total spend for the whole smoke test: 14.05 credits of the 5,000 pool (about 0.28%).

### Credit rates (from the Gnani dashboard's Usage History)

| Service | Measured | Notes |
|---|---|---|
| Prisma v3.0 STT | about 0.45 credits per minute of detected speech | 6.58 billed minutes cost about 3 credits |
| Timbre v3.0 TTS | about 2.7 credits per 1,000 characters | 4,097 characters cost about 11 credits |

- **Silence is free.** A run that streamed 303 s of audio but contained only my 23 s of speech was
  billed 0.4 minutes. Prisma bills detected speech, not connection time.
- **TTS is billed per character, not per byte.** The Devanagari test sent 1,024 characters (2,536 UTF-8
  bytes) and was billed 1,024.
- Audio tags such as `<coughs>` should count as characters too (the docs say so); not yet isolated in a test.
- Billing is only visible on the dashboard. Stream messages carry no usage fields.

### Prisma v3.0 (realtime STT)

- **One language per session.** The docs say a list of 1 to 4 codes works. The live server rejected
  `hi-IN,en-IN` with "exactly one locale is required". Use `hi-IN` (or `indic-auto`). The streaming docs
  also say `multi_lang_codes` "is not accepted on streaming", so the error message's hint does not apply to us.
- **No partial transcripts that we know how to turn on.** Transcripts arrive once per finished utterance.
  The `connected` message contains `partials_enabled: false` and `partial_interval_ms: 450`, but no docs page
  mentions partials or any way to enable them, and we did not guess a header. We design without partials.
- **Settings go in WebSocket upgrade headers**, not a JSON start message. The default model is v2.5 unless
  `model: prisma-v3.0` is sent. Audio is 1,024-byte frames, one every 64 ms at 8 kHz, paced in real time.
- **Sample-rate mismatch, unresolved.** We send `x-sample-rate: 8000`. The docs say the `connected` message echoes
  it, but the server reported `16000`. The reported audio length (23.6 s for a 23.2 s clip) and the accurate
  transcripts show that 8 kHz audio was handled correctly, so it works in practice. We do not know why the
  echo says 16000.
- **Server latency about 0.9 s for a 23 s utterance**, plus about 0.5 s of end-of-speech silence the VAD waits
  for, so roughly 1.3 s from end of speech to transcript. It grew with utterance length (about 1.8 s for 150 to
  200 s utterances).
- **Long unbroken speech delays the transcript** until the VAD hears a pause (first transcript after 148 s in the
  5-minute test). Documented mitigations: `x-max-speech-s` (5 to 120 s) and `{"type":"commit"}`. Both are
  tested by `scripts/smoke_latency.py`.
- **Digits and letters (n=1 clean clip, not a benchmark).** On one clean recording, the phone number and
  the 12-digit account number came back exactly right (as Devanagari digit words in `codemix` format). Letters were
  weaker: "mirageupi" came back as "miraj UPI", and IFSC letters came back wrong. This is one clip, one voice,
  so it only tells us where to look. E1 will measure it properly.

### Timbre v3.0 (TTS)

- **Time to first audio: 0.28 to 0.50 s**, including a fresh connection each time. Audio is generated about
  10 times faster than real time.
- **Only one request at a time.** A second one gets a 429, which matters when a filler clip and a reply overlap.
- **Hinglish in Latin script (`hi-en`) sounded best** to me, so Rajesh Uncle's lines will be written in
  Latin-script Hinglish. The voice (Yashraj) and the audio tags are not judged yet; `--case voice-line --voices ...`
  compares voices.

### Gemini free tier

Real limits from AI Studio (the earlier "20 per day" chart was only Gemini 3.8 Flash):

| Model | Per minute | Per day | Note |
|---|---|---|---|
| Gemini 3.1 Flash-Lite | 15 | 500 | |
| Gemini 3.5 Flash-Lite | 15 | 500 | used up on 1 Nov |
| Gemini 2.5 Flash-Lite | 10 | 20 | |
| Gemini 3.6 Flash | 5 | 20 | |
| Gemini 3 Flash | 5 | 20 | |
| Gemini 2.5 Flash | 5 | 20 | |
| Gemini 3.8 Flash | 5 | 20 | used up on 1 Nov |
| Gemma 4 26B and 31B | 30 | 14,400 | 16K tokens per minute; Hindi quality untested, so off |

Gemini models allow 250K tokens per minute. The counters belong to my Google project, so requests made
elsewhere also count. The daily reset time is not confirmed (believed to be midnight Pacific).

- **`gemini-3.1-flash-lite` confirmed working** with `scripts/smoke_gemini.py` (two requests, HTTP 200). That
  confirms the key, the `x-goog-api-key` header and the model ID string.
- **`gemini-3.8-flash` timed out** (`ReadTimeout`, 15 s timeout) instead of returning a 429. The cause is
  unknown: it could be a slow model, a request that hangs when the quota is exhausted, or a timeout that is too
  short. Not a quota finding. To be retried with a longer timeout.
- **`classify_429` is still a guess** until we have a real 429 body. The first one seen is saved to
  `data/llm_first_429.txt`.
- Consequence: the call must work from pre-written lines alone, and Gemini is only for moments the lines
  cannot handle.

## Latency test: 7 short sentences, ~1 s pauses (1 Nov 2026, evening)

Delay = time from the end of speech (found from loudness in the WAV) to the transcript arriving.
One recording, one speaker, 7 sentences per run, so these are indicative only.

| Variant | Median delay | Range | Note |
|---|---|---|---|
| baseline (`x-min-silence-ms` 500) | 0.98 s | 0.87 to 1.16 s | 7 sentences, 7 transcripts |
| `x-min-silence-ms` 300 | about 0.70 s (hand-calculated) | 0.68 to 1.00 s | **split sentence 1 at its comma pause**: 8 transcripts, "नमस्ते।" then the rest |
| baseline + `commit` 200 ms after speech ends | 0.56 s | 0.53 to 0.77 s | includes our own 200 ms wait; no split |
| `x-max-speech-s` 6 on a 23 s unbroken clip | first transcript at 6.7 s, then every ~6 s | | forced cuts land mid-sentence and **split a phone number across two transcripts** |

- Server processing for short utterances was only 240 to 450 ms (the 0.9 s seen earlier was for a 23 s utterance).
  Most of the baseline delay is the 500 ms of silence the VAD waits for.
- **Known bug in `scripts/smoke_latency.py`:** it matches transcripts to sentences in order, so one split
  transcript shifts every row after it. The delay column printed for the 300 ms run is therefore wrong; the
  0.70 s above was hand-calculated from the transcript times. We will fix this when we build the real E2
  harness, not before.
- **"About 1.0 s reply" is a design target, not a number to publish.** It is a best case: one clean recording,
  template lines and cached audio (so no LLM or TTS wait). We publish only what E2 measures.
- `commit` needs us to detect the end of speech ourselves (a client-side detector), which does not exist yet.
- Digits came back both as Devanagari words and as English words ("zero zero"), and "case number" came
  back as "केस नंबर" in some runs: the extractor must accept all of these.
- Letters were weak again on the same clip ("miraj UPI", "N A I R G"). Still n=1 clean clip, not a benchmark.
- Voice audition (Yashraj, Prakash, Devansh, Avinash, one 129-character Hinglish line): first audio 0.30 to
  0.39 s, about 7.5 s of speech each. By ear, Prakash and Avinash both sound natural; not picked yet.

### Open items from the latency test

- The `connected` message still reports `sample_rate: 16000` although we send `x-sample-rate: 8000`. Unexplained;
  transcripts are fine.
- Later: try a larger `x-max-speech-s` (about 20 to 30 s) purely as a safety cap against endless monologues,
  not to cut speech in normal use.
- Later: a client-side end-of-speech detector plus `commit` (measured 0.56 s median, including our 200 ms wait),
  as an optimisation with its own evaluation.

## Decisions

- **No 1.5 s p50 promise.** The blueprint's target is not reachable with about 1.3 s of STT latency alone before
  the LLM and TTS. I will not promise a number; I will publish the p50 and p95 we measure (E2), with the method.
- **STT setting: `x-min-silence-ms` 500 (the default).** 300 ms split a sentence at a comma, and the 6 s cap
  split a phone number across transcripts, so neither is used.
- **Voice: Prakash**, audio tags used sparingly, normal speed. By ear, Prakash with tags at normal speed
  sounded the most natural. Speed 0.9 was tried and not chosen. No pre-rendering until the template lines are
  settled, so we do not pay twice.
- **Silent filler clips** will carry perceived responsiveness while the real reply is prepared.
- **Fake identifiers**: invented varied digits, an unreal UPI provider (`@mirageupi`), a fake IFSC bank code.
  The "mobile numbers never start with 5" claim is unverified (no source found), so the convention is still open.
- **Evaluation E3 (an LLM playing the scammer) is dropped** because of the Gemini daily limit.
- `docs/blueprint.md` is kept out of the public repo.
