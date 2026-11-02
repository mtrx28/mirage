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
- **`gemini-3.5-flash-lite` answered in 0.91 s even though AI Studio showed 501 of 500 used that day.** So my
  earlier note that it was "used up" was wrong. We cannot tell whether the dashboard count blocks anything,
  counts differently, or whether the window had already rolled over. Our own daily budgets stay conservative
  (450 of 500, 18 of 20), the reset-time guess (Pacific midnight) is unconfirmed, and **a real 429 is the only
  truth**.
- **`gemini-3.8-flash` timed out at both 15 s and 30 s**, while both Flash-Lite models answer in about a second.
  Cause unknown. Treated as unusable for live calls. 3.6 Flash is the same family and untested.
- **Default model list is now 3.1 Flash-Lite, then 3.5 Flash-Lite.** 2.5 Flash-Lite and 3.6 Flash stay in
  `config.py` as commented-out options, "untested, 20 per day".
- `smoke_gemini.py` has sent 5 of its maximum 6 requests; the last one is not being used.
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

## extract.py: facts used and their sources (1 Nov 2026)

| Fact | Status |
|---|---|
| Indian mobile numbers are 10 digits starting 6, 7, 8 or 9 | Sourced: Wikipedia, "Mobile telephone numbering in India". Secondary source; TRAI's own plan not read. |
| IFSC is 4 letters, "0", then 6 letters or digits | Sourced: Wikipedia and several bank pages agree; a few pages differ on details. Secondary. |
| Bank account numbers are 9 to 18 digits | **Unverified.** Finance blogs repeat it; I found no RBI rule. Used only as a loose shape check. |
| Known UPI handles (`ybl`, `oksbi`, `okhdfcbank`, `okicici`, `okaxis`, `paytm`) | **Unverified.** No official NPCI list found; the list is small and from memory. Our invented `mirageupi` lives in a separate synthetic-mode list. |

**Do not quote the extract test count as accuracy.** The round-trip tests only prove the extractor agrees with its
own digit table. They say nothing about how well Prisma hears digits; that is what E1 will measure.

Design notes: the extractor never replaces what was heard (guesses go in `suggested`), every finding carries its
quote, arrival time and source index, and joining across transcripts uses when the next speech began
(arrival time minus its audio length), because a forced or silence-based split delivers the second half only
after it has been spoken.

## The brain: persona and lines (1 Nov 2026)

- `content/persona.json` is Rajesh Uncle's fact sheet. Every value is invented and flagged. **New synthetic phone
  numbers start with 0000** (Uncle's is `0000274915`). A 00 start is the international dialling prefix, so
  `0038274915` would read as +38 274915; the recorded clip number `0047138296` has the same pattern (+47, Norway).
  As far as I know no country code starts with 0, so 0000 cannot be read as an international number. **That
  reasoning is UNVERIFIED (no source found).** The old clip stays as a fixture; new test strings use 0000. The
  persona checker enforces the 0000 start. The account number is 14 digits so it cannot pass for an
  Aadhaar-style 12-digit number. There are no Aadhaar, PAN, OTP, PIN, password or card facts in the sheet.
- **The fake IFSC prefix `MIRG` is UNVERIFIED as "not a real bank code".** A search on 1 Nov 2026 found no bank
  with that prefix (it only appears inside branch names), but I could not check RBI's full list.
- `content/lines.json` has 58 template lines in 14 tactics. A checker (`python -m mirage.lines`, also run as
  tests) enforces: 40 to 80 characters with slots filled, 4 to 6 variants per tactic, at most one tag per line
  (7 lines carry one), no numerals or "@", **zero findings when each line is run through `extract_intel`**
  (so "ek minute" is fine but no real number can get in), no claim to be a person, human, robot or AI, no request
  to the caller for money or credentials, and no "I already sent/installed/clicked/shared". These are safety
  nets; the owner still reads every line. The tests also plant bad lines to prove the checker catches them.
- After the owner's review: "beta" was in 23 of 58 lines (it would sound scripted), now in 13, and the checker
  enforces at most a third. Register is decided on purpose: polite forms (aap, boliye, bataiye, dijiye, ruk jaiye)
  to everyone because the caller claims to be an officer, with "beta" as the only familiar word; informal forms
  (bolo, batao, ruko, dekho, tum ...) fail the checker. "darja" became "post". Obstacle tactics now carry a
  `trigger` in `lines.json` (`app_not_opening` after an install request, `otp_late` after a code request,
  `limit_exceeded_second_account` after payment instructions) and `give_own_detail` is only used when the caller
  asks for one of Uncle's details; the FSM will enforce these.
- **Until the safety gate exists, a sincere "are you a person or an AI?" or a detected emergency must not get a
  stall line.** The FSM returns a reply plan marked `needs_gate` with no text, and the runner prints that
  clearly. The gate piece will define the real behaviour.
- **No voice call is to be wired up until the gate exists.**
- The LLM fallback stays off by default in both the FSM and the runner. Its latency (a Gemini call plus the digit
  wait) is not measured, so it is turned on only after E2.
- Two stage-detection test sets will be reported separately: my invented set ("the rules do what I designed")
  and the owner's own, written without looking at the keyword lists (`tests/fixtures/stage_cases_user.txt`,
  "closer to a real caller"). Neither is an accuracy figure.

## The brain: state machine and runner (1 Nov 2026)

- `mirage/fsm.py` (`CallBrain`), `content/stage_rules.json`, `mirage/spoken.py`, `scripts/run_call.py`. The FSM picks the
  stage and the tactic; the line comes from `content/lines.json`. The LLM fallback is **off by default** (in the FSM and
  in the runner) and is tried only when no template tactic applies and the caller asked Uncle something no signal
  covers. Its latency, a Gemini call plus the digit wait, is unmeasured, so it stays off until after E2.
- **Stage rules:** weighted keywords for the current line (the previous line adds 0.3 of its weight), findings add a big
  boost (a payment identifier -> S3, a case number -> S1), a minimum score of 2, a stickiness bonus for the current stage
  (none for S0), S4 enterable only from S3/S4, one-step moves back only with a margin of 3. **A line with no keyword and
  no finding never moves the stage and gets a generic stall.** All weights are guesses, not measurements.
- **Two stage test sets, reported separately and neither an accuracy figure:** my invented set (41 lines, all match; it
  was written with the keyword list in view, so it shows "the rules do what I designed") and the owner's set in
  `tests/fixtures/stage_cases_user.txt` (still empty; "closer to a real caller"). Both are scored on the words of a
  single line.
- **Triggers are enforced.** `app_not_opening`, `otp_late` and `limit_exceeded_second_account` fire only after the caller
  asked for an install, asked for a code, or gave payment details, each at most once per call, and not in S0 or S5.
  `give_own_detail` is used only when the caller asks for one of Uncle's details; a credential Uncle has none of
  (Aadhaar, PIN, ...) gets a stall.
- **Digit mode** waits 1.8 s (config), after Uncle asks for digits or when the caller's last words are a half-finished
  number. Transcripts within 2.5 s are joined by `extract_intel`. After Uncle asks for a case or account number the
  extractor gets `expected` once.
- **Read-back** says the first half of a caller's number (3 to 6 characters, never all of it), spoken in groups of 3, and
  each reply records where every digit came from (the fact sheet field, or the finding's quote, transcript index and
  time). At most two read-backs per kind per call; the same number is never read back twice.
- **`needs_gate`:** a possible "are you a person or an AI?" or an emergency word returns a plan with no text. The
  keyword lists over-trigger on purpose (a bare "ai" or "machine"); the gate will decide what is sincere.
- **Spoken digits are UNVERIFIED with Timbre.** The TTS text-normalisation guide says Timbre reads bare digit runs digit by
  digit near words like "account", reads 10-digit numbers in groups of 3-3-4, and always spells IFSC, OTP, UPI and KYC
  letter by letter. It does not say what happens to number words ("ek do teen"). The default is words; the alternative is
  spaced numerals (`FSMConfig.digit_style`). Compare both on one small Timbre test before any pre-rendering.
- Read-back lines run past 80 characters once spoken digits are in them. That is expected (they are always synthesised live).

### First real-caller measurement of the stage rules (2 Nov 2026), recorded BEFORE any change

The owner wrote 27 caller lines (`tests/fixtures/stage_cases_user.txt`) without looking at the keyword list. Scored on
the words of each single line, **11 of 27 matched**. That is the honest "closer to a real caller" number for the rules as
first written; it is not an accuracy figure (27 lines, one writer, one line at a time). After fixing two defects in how
the TEST scored lines (a tie went to the wrong stage, and the extractor's findings were ignored) the same rules scored
13 of 27. Of the 14 misses: 6 had the right stage as their best guess but scored below the minimum of 2 (a single
keyword weighing 1.5), 2 went to the wrong stage with enough score (one more hook word than fear words; "hold" read as
S1 authority), 3 had no keyword at all, and 3 had a keyword for a different stage as their best guess. **From the moment keywords are
changed after reading these lines, this set is a development set and no longer a fair test**: a fresh held-out set
(`tests/fixtures/stage_cases_heldout.txt`) is reserved for the next honest measurement.

Bugs found by the same runner session (fixed; none of them involved fitting keywords to the owner's lines):
a tie between stages went to the earlier stage in the test helper but to the later one in the brain; a line with a weak
keyword of the stage the call was already in (one hook word in S0) got a generic stall instead of that stage's tactic; the
stage test ignored the extractor's findings that the brain uses; the signal for "tell me your address" missed
"aap kahan rehte hain" and "confirm".

### Stage keywords tuned (2 Nov 2026); owner set 1 is now "OWNER set 1 (dev set, tuned, not a test)"

| Measured on | Before tuning (after the two scoring fixes) | After adding general words |
|---|---|---|
| Owner set 1 (27 lines), one line at a time | 13 of 27 (11 of 27 untouched, before any change) | **25 of 27**, but this set was in view while tuning, so this is NOT a test result |
| Invented set (41 lines) | 41 of 41 | 41 of 41 |
| Replay of the owner's three practice calls (27 turns) | 8 generic stalls; call 2 reached only S4, call 3 only S4 | 1 generic stall; call 2 now reaches S2, S3, S4, S5 and call 3 reaches S5 |

- **What was added:** general words, not whole lines: S0 department, delivery, pending, order; S1 suspicious, complaint,
  inquiry, investigation, illegal, fraud, against, expire(d), pakda, seized, notice, station, penalty (and two
  Devanagari ones); S2 "hold kijiye", rukiye, rahiye, network; S4 "jaldi kijiye", "jaldi karo", offer, limited, khatam;
  S5 "call cut", "call band", chhodo, "dobara call". Weights are guesses.
- **The minimum score stays at 2.0.** Lowering it to 1.5 was measured: before tuning it lifted set 1 from 13 to 19 of 27 but
  broke two invented cases (a lone "jaldi" claimed S4 and a lone "case" claimed S1), and it barely changed the stage path of
  the three practice calls (it only turned 3 stalls into tactics). After tuning it added just one line (26 of 27) and still
  broke the same two. Not worth it.
- **Still missed, left alone on purpose:** "Aapka bank KYC expire ho gaya hai" (hook word and fear word tie at 1.5, below the
  minimum) and "Hello hello sir, sun rahe ho kya" (a greeting that looks like "are you there?"). A single line like these
  carries no clear stage; claiming none is the safe answer.
- **What leaked from set 1, precisely.** Before this tuning, no stage weight was tuned on set 1. Three things used it:
  (1) two lines informed the own-detail signal cues ("Ab apna account number confirm kijiye", which added the request
  words "confirm", "verify", "enter", and "aap kahan rehte hain?", which added the direct cues "kahan rehte", "kis bank",
  "naam kya"); (2) reading its results led to fixing two defects in how the TEST scored lines (a tie went to the wrong
  stage; the extractor's findings were ignored), which moved the score from 11 to 13 without changing any keyword;
  (3) this tuning, which changed the keywords with the set in view. **Set 1 is frozen** (a test checks its content
  hash). The honest "closer to a real caller" number will come from the held-out set
  (`tests/fixtures/stage_cases_heldout.txt`, still empty; written by the owner, never tuned against).
- **One scoring function:** the brain and the stage tests both call `score_line`, so they cannot drift apart again (a test
  proves it and compares the brain's scores with the helper's on every test line).
- **Amounts:** a number next to a money word (rupaye, rupees, Rs, INR, the rupee sign, lakh, hazaar, crore) is recorded as
  kind `amount` with its quote and time, not as an `ambiguous` number. It is never read back or classified. Only a digit
  directly followed by the unit counts, so "..., Rs 500" does not turn the account number before it into an amount.
  Amounts written fully in words ("pachas hazaar") are still not parsed: only "do lakh" gives "2".
- **needs_gate matching is on whole words** (a test with 21 ordinary Hinglish and Devanagari lines, including hai, mai,
  kai, bhai, है, कई, must not trigger it, and 11 real questions and emergencies still do). "A.I." and "ए आई" were added.
- `scripts/smoke_digits_tts.py` (not yet run) compares four ways of writing a digit read-back for Prakash (digit words,
  spaced numerals, plain numerals, Devanagari digit words), 242 characters, about 0.65 credits. After listening, the
  owner decides `FSMConfig.digit_style`.

## Later list

- Spelled-letter patterns like "M for Mohan" (skipped for now; STT letters are the weak point, E1 will measure it).
- FSM, digit-dictation state: wait a longer, configurable silence (start around 1.5 to 2 s) before replying, and
  join transcripts that arrive close together (`join_window_s` in `extract_intel`, 2.5 s to start).
- `safety.py`: base the output gate on where digits came from (Uncle's fictional fact sheet or the caller's own
  transcript, via a Finding's quote), not only on patterns, because Uncle must be able to read back the caller's digits.
  `looks_like_real_mobile` is only a pattern check on whole digit runs.
- Client-side end-of-speech detector with `commit` as an optimisation, with its own evaluation.
- A larger `x-max-speech-s` (about 20 to 30 s) as a safety cap only.
- Amounts in words ("पचास हज़ार") are not parsed.

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
