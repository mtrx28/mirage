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

- **Daily limit seen: 20 requests per day** (AI Studio rate-limit chart; the chart did not say which model).
  Per-minute limits are 5 to 15 for the Flash and Flash-Lite models. Per-model daily numbers still to be read
  from AI Studio, and the reset time (believed to be midnight Pacific) is still to be confirmed.
- Consequence: the call must work from pre-written lines alone, and Gemini is only for moments the lines
  cannot handle.

## Decisions

- **No 1.5 s p50 promise.** The blueprint's target is not reachable with about 1.3 s of STT latency alone before
  the LLM and TTS. I will not promise a number; I will publish the p50 and p95 we measure (E2), with the method.
- **Silent filler clips** will carry perceived responsiveness while the real reply is prepared.
- **Fake identifiers**: invented varied digits, an unreal UPI provider (`@mirageupi`), a fake IFSC bank code.
  The "mobile numbers never start with 5" claim is unverified (no source found), so the convention is still open.
- **Evaluation E3 (an LLM playing the scammer) is dropped** because of the Gemini daily limit.
- `docs/blueprint.md` is kept out of the public repo.
