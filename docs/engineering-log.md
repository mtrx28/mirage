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

### First held-out measurement of the stage rules (2 Nov 2026): OWNER set 2, 4 of 18

The owner wrote 18 lines after the keywords were tuned, without looking at the rules. Scored one line at a time: **4 of 18
matched (22%)**. Of the 14 others, **12 claimed no stage** (the call stays where it is and Uncle stalls) and **2 claimed a
wrong stage** (both "customs ... parcel" lines scored S0 although they were fear or payment). So the rules mostly stay
quiet rather than err, but they are weak on lines they have not seen. This is the honest "closer to a real caller" number;
it is not an accuracy figure (18 lines, one writer, no call history).

- By label: S0 3, S1 4, S2 2, S3 4, S4 3, S5 2. Right: one S3, two S4, one S5, none for S0, S1 or S2.
- Of the 12 no-claim lines: 5 had the right stage as their best guess but scored below the minimum (one 1.5 keyword), 5 had no
  keyword at all, and 2 had a keyword for another stage.
- **Seven of the 18 lines are about scams outside the two scenarios Mirage is built for** (electricity cut-off 3, TRAI /
  SIM-block 2, a relative in trouble 2). The rules have no vocabulary for them and the template lines were never written
  for them, though S2 to S5 tactics are scenario-neutral.
- **Six lines use informal verb forms** (ruko, kholo, karo, bhejo, kaato, "tumhare"); the keyword lists were written in
  polite forms (kijiye, kholiye, bhejiye).
- Compared with set 1: tuning lifted set 1 from 13 to 25 of 27 but set 2 scored 4 of 18, which is what fitting to a seen set
  looks like. The gap is the reason the held-out set exists.
- **No rule has been changed in response.** Any change from here on is made with set 2 in view, which makes it a development
  set too; a third file would then be needed for the next honest number.

### Verb stems, a blueprint check of the weights, and scope (2 Nov 2026); OWNER set 2 is now a dev set

**What changed.**
- **Verb stems** (matched as the start of a word, so every politeness form counts), a short explicit list of verbs with a
  distinctive stem, each below the minimum score so one stem can never move the call alone: S3 `bhej` (send: bhejiye, bhejo,
  bhejna, bhejni; weight 1.5), `jama` (deposit; 1.5), `daal` (put money in; 1.0, and it also means lentils, which is harmless
  alone); S2 `ruk` (wait: ruko, rukiye; 1.0); S4 `khol` (open an app or link; 1.0); plus the Devanagari forms. Stems of very common verbs (kar,
  bol, de, le, ho, bata, dekh ...) and anything shorter than three characters are refused by the loader, because they would
  match half of all Hindi sentences. The exact words `jama`, `rukiye`, `bhejiye`, `bhej do` were removed so they are not
  counted twice. The minimum score stays at 2.
- **Blueprint check of the weights** (against the stage tables in the blueprint, not against the owner's lines). Found and
  changed: `drugs`, `narcotics` and `passport` are the HOOK ("a parcel holds drugs, passports or SIM cards"), so they moved
  from S1 to S0, with `sim card` added; `funds` added to S3 ("verify funds"); `call transfer` added to S1 ("transfers you to an
  officer"). Consistent already: parcel, courier, fedex, dhl, kyc and "bank se" (hook), cbi, police, officer, warrant, fir,
  case (authority and documents), upi, ifsc, payment (payment), arrest and jail (fear). Not changed, noted: the blueprint's
  isolation stage ("don't tell family", "don't hang up", Skype or WhatsApp video) has no stage words, and the FSM table has no
  isolation state.
- **Conflict left open: `customs`.** The blueprint lists customs among the authority officers (S1), but the rules count it as a
  hook word (S0, 1.0). Moving it to S1 improved one set 2 line and removed one wrong claim, but it broke one invented line that
  passed before ("customs se parcel ke baare mein call hai", written when I assumed customs was a hook). The rule was to revert
  anything that breaks a passing line, so it is reverted. If the invented line's expectation is changed, `customs` can move.

**Regression check, before and after** (same rules loader, invented set, owner sets 1 and 2, replay of the three practice calls):

| | Before | After | Notes |
|---|---|---|---|
| Invented set (41) | 41 | 41 | nothing worse |
| Owner set 1 (27) | 25 | 25 | nothing worse |
| Owner set 2 (18) | 4 | **5** | the one new line is out of scope ("tumhare relative ... amount abhi bhejo", from the `bhej` stem) |
| Owner set 2, in scope (11) | 4 | **4** | the fix did not help the covered scam types |
| Owner set 2, out of scope (7) | 0 | 1 | |
| Replay of the 3 practice calls (27 turns) | 1 generic stall | 1 | stages and tactics identical |

The stems helped one line and did little else: most misses were a single keyword at 1.5 or less, or no keyword at all.

**Scope.** Electricity cut-off, TRAI or SIM-block and relative-in-trouble calls are out of scope (the blueprint covers digital
arrest, fake agencies, courier and bank KYC). Seven of the 18 set 2 lines are about them; the report prints them separately
and leaves them out of the in-scope score. Written up in `docs/safety.md` as "only the scam types implemented are covered".

**Set labels from now on:** set 1 "dev set, tuned, not a test"; set 2 "dev set after the fix; untouched score was 4 of 18"
(frozen by hash, like set 1); set 3, `tests/fixtures/stage_cases_heldout_friend.txt`, written by the friend who plays the
scammer, empty, "held out". The report prints only counts for set 3 until the owner says its misses may be shown.

### `customs` moved to authority (2 Nov 2026): a rules change made with set 2 in view

At the owner's decision (the blueprint is the reference and the invented test line was the wrong part): `customs` moved from
S0 (1.0) to **S1, weight 1.5**, below the minimum of 2, so "parcel" (S0) or "case" (S1) decide the stage when it appears in a
line. The invented line "customs se parcel ke baare mein call hai" now expects **no stage claimed**, not S0: "parcel" alone
is 1.5, below the minimum, so it cannot carry the line. **This change was made with set 2 in view, so set 2 stays labelled a
dev set** (it was already frozen as one).

Regression check against the rules before this change:

| | Before | After | What changed |
|---|---|---|---|
| Invented set (41) | 40 of 41 against the new expectation (the old line fails) | 41 | only the one line whose expectation changed |
| Owner set 1 (27) | 25 | 25 | nothing |
| Owner set 2 (18) | 5 | **6** | "customs mein aapka parcel rok diya gaya hai aur case banega" now S1 (right); "parcel release ... customs fee bhejni padegi" went from a wrong S0 claim to no claim |
| Replay of the 3 practice calls | 1 generic stall | 1 | stages and tactics identical |

Nothing that passed before fails now. Set 3 (the friend's) was not read or scored.

### Set 3 (friend, held out): 12 of 20 (2 Nov 2026)

**set 3 (friend, held out): 12 of 20, scored once, no rules changed afterwards, 3 more had the right stage as their best
guess below the minimum.** The `customs` change (moved to S1) was made before set 3 was scored, and the file's content was
never edited (it was written once by the friend and scored once). The missed lines are not printed in the report and stay
unprinted. This is the honest "closer to a real caller" number for the rules as they stand; it is not an accuracy figure.

Sample-size caveats for reading sets 2 and 3 side by side: (1) **they have different writers** (set 2 the owner, set 3 the
friend), so a higher or lower score may be partly the writer's style, not the rules; (2) **7 of set 2's 18 lines were out of
scope** (electricity, TRAI, relative in trouble), which is why set 2 is also reported as in-scope 5 of 11; set 3 was asked to
cover only the implemented scam types; (3) both sets are tiny (18 and 20 lines), so one line is 5 to 6 points.

### Digits, by listening (2 Nov 2026): spaced numerals

The owner listened to the four Prakash versions from `scripts/smoke_digits_tts.py` (digit words, spaced numerals, plain
numerals, Devanagari digit words). Devanagari and spaced numerals sounded most natural; **spaced numerals ("0 0 4, 7 1 3") were
chosen**. It is a single sample of each version, one listener, one voice, one phrase: a preference, not a measurement, and
it says nothing about how intelligible the digits are to a caller on a phone line at 8 kHz. `FSMConfig.digit_style` and
`mirage/spoken.py` now default to `"spaced"` (the other value is `"words"`); the old name `"numerals"` is gone. Every
spoken digit string uses the configured style: Uncle's own details from the fact sheet (phone, account, IFSC, the flat
number in the address) and read-backs of the caller's digits, and a test checks this. Replies now contain numerals, so the
safety gate will compare digits after stripping spaces and commas.

### Safety gate, first stop (2 Nov 2026)

Built: `mirage/safety.py` (output checks, resolution of `needs_gate`, a provisional scam score), `content/gate_lines.json`
(the gate's own lines, with their own lint: `python -m mirage.safety`), `content/gate_rules.json` (abuse and threat word lists,
a starter list for the owner to extend), a `GateConfig` in `mirage/config.py`, and the hook wired into the FSM as
`gate(plan, state, context)`. The runner uses the gate by default; `--no-gate` shows the raw brain. Not built yet (second
stop): the exit rule for callers who do not look like scammers, the calibration printout, the red-team runner and the
README disclosure notes.

- **Every reply is checked before it is spoken** (templates, persona answers, anything a model writes): each digit must trace
  to the fact sheet, the caller's own findings (quote and index), or the gate's emergency number, and the gate re-verifies
  those sources instead of trusting them; spaces and commas are stripped before comparing; no "@" or link; no real-looking
  mobile, Aadhaar-style or PAN number unless it is the caller's own; no claim to be human or to be "not an AI"; no request for
  money or credentials; no "already sent or installed"; no impersonating an official or threatening; persona intact (Latin
  script, no mention of prompts or models, tags as allowed, length); an obstacle line needs its trigger. A failed check is
  replaced by a neutral stall, and if even that fails the gate says nothing and ends the call.
- **Identity asks:** a question about the speaker (an AI or human word together with "you", "this" or a cue like "ho kya").
  With a scam score below `identity_disclose_below` (1.0, a guess) the first ask gets the honest answer and the call ends;
  with more evidence Uncle gives up to `max_deflections` (2) in-persona deflections, then the honest answer. Emergencies exit
  at once, at any time, with "112". Abusive or violent callers get calm answers, then a goodbye (or the honest answer if there
  is little scam evidence). Arrest threats are scam content, not hostility.
- **112 is UNVERIFIED.** I found only secondary sources (news reports and a state police page) that 112 is India's single
  emergency number under the ERSS system; I did not find an official MHA or PIB page. Verify before relying on it. It is a
  config value (`emergency_number`).
- **The scam score is provisional.** Its formula is in `GateConfig` and in the plan, every weight is a guess, and it is
  used only for the identity and hostility decisions so far. It rests on the keyword layer (set 3: 12 of 20), so it will
  under-count real scams; calibration against the practice calls and the friend's 5 scam and 5 benign scripts is the second stop.
- **Two bugs the gate tests found in my own code:** the gate treated the goodbye turn itself as "the call already ended" (the FSM
  marked the call ended before calling the gate), and it blocked Uncle's legitimate spoken address ("Flat 1 2, Mirage
  Apartments") because the project name in the fact sheet was flagged as a meta word. Both fixed; a test now runs a whole
  scripted call and requires every turn to pass the gate unchanged.
- **Unresolved:** a deflection is an in-persona evasion, not a denial; no legal opinion has been taken on it and disclosure
  rules vary by place (this goes in `docs/safety.md` at the second stop).

## Voice path, stop 1 (microphone to Prisma to live transcript)

Built and tested against an in-process stand-in for Prisma. **Nothing here has run against the real service yet**; the
manual check is the first real run. No audio rendered, no Gemini.

- **Docs re-read** (docs.gnani.ai/api/STT/stt-websocket.md). What they say, and where it differs from the plan:
  - `keyterms` (Prisma v3.0): up to **200** words or phrases, JSON array or comma-separated. **Non-ASCII must be
    JSON-escaped.** An invalid list is **silently ignored**, so a wrong list cannot be seen as an error; we can only
    see it work by the transcripts. The page states **no per-term length limit and nothing about billing**.
    `bias_list` is the older name, and `bias_score` is v2.5 only. We send a JSON array of ASCII terms
    (`content/keyterms.json`, 57 terms, checked by a loader).
  - **Reconnecting: the docs say nothing about it.** They say header settings are fixed per connection ("changing
    them requires reconnecting"), a stream ends after **15 minutes** (an `error` message, then close code 1001), the
    idle timeout is 60 s by default (range 5 to 900; we set 300), and there is a **concurrent-session cap per
    organisation** (a rejected connection gets an `error` naming the limit). Whether a reconnect starts a new
    15-minute clock, and what happens to audio sent while reconnecting, is not documented. Policy: one retry, no audio
    replay, and never a retry after 1001 or 1008.
  - **Differs from my plan:** the docs list a `speech_start` and a `processing` message ("VAD detected end of speech")
    and `commit`. I had said Prisma sends no speech-start event; wrong. The smoke runs already contain `speech_start`;
    `processing` has not been seen in any saved run. Stop 1 only logs them. If `processing` arrives it gives the
    end-of-speech time for the latency log without our own detector. Barge-in could use `speech_start` later.
  - Also confirmed: audio frames are exactly 1,024 bytes, real-time pace, bursts "degrade VAD".
  - Unconfirmed in practice: `keyterms` has never been sent in a real run, and `connected` still reports
    `sample_rate: 16000` while we send 8000.
- **Built:** `mirage/audio.py` (resample with soxr, exact frames, a paced sender), `mirage/prisma.py`,
  `mirage/session.py`, `mirage/server.py`, `mirage/credits.py` (ledger, sync command), `mirage/calllog.py`,
  the page in `mirage/web/`, `content/keyterms.json`, and `VoiceConfig` in `config.py` (every non-Prisma number is a guess).
- **Gate required:** `CallSession` and `create_app` raise `GateRequired` without a gate, and `main()` builds a real
  `Gate()`. Tests: no gate, no session; no gate, no server; `main()` passes a `Gate`; an AST scan fails if any module in
  the package builds a `CallBrain` without a real `gate=`. Mutation-checked.
- **Resampler:** the soxr default setting held back about 70 ms of audio at the start and the highest 130 ms; we use
  "MQ", about 35 ms. A constant start-up delay, not drift.
- **Credits:** estimated in code. Speech-to-text takes the larger of (seconds a simple loudness check calls speech) and
  (seconds Prisma reports in transcripts), at 0.45 per minute; this leans high. The ledger is seeded at 18.37
  credits and 23 requests (the dashboard figure), written every 15 s during a call, and compared against a hard stop
  at 80% of 5,000 (4,000), a warning at 70%, and a per-call cap of 40 (guess). `python -m mirage.credits sync <figure>`
  resets it to the dashboard. An unreadable ledger blocks calls until synced.
- **Bug the tests caught:** a clean close from Prisma (the 15-minute close, code 1001) ends the receive loop without an
  exception, so the close code was lost and the link would have retried. It now reads the code from the socket.
- **Access control:** loopback address only, an Origin check on the call WebSocket (another website open in the browser
  cannot start a call), one call at a time, one server process (an OS file lock).
- **Not built yet (by design):** brain, gate use on replies, Timbre, playback, half-duplex mute (the Pacer can mute, but
  nothing sets it), the stage and intel panels, latency timestamps beyond logging `speech_start` and `processing`.
- **Inbound only, in the voice path:** the page has no number entry and no dialer; the only audio source is this
  computer's microphone; the only outbound connection is to Gnani's speech API (Prisma now, Timbre later), which is a
  speech service and not telephony. A test checks the page for outside addresses and phone-call APIs, and the existing
  scan covers the new files.
- **Friend session note (for the half-duplex version in stop 3):** wear headphones for Uncle's voice and talk into the
  laptop microphone. Half-duplex is a limitation: while Uncle speaks, the caller's audio is replaced by silence, so
  talking over him is lost.

## Voice path, stop 1: the first two real runs (2026-11-02)

Both runs are in `data/calls/` (gitignored). Findings from the local files only; no network, no audio.

**These two runs cannot be compared for accuracy or cost.** They contain different amounts of speech: run 1 had 19
Prisma segments (26.8 s of segment audio with text) and run 2 had 8 (16.9 s), and in run 2 Prisma's VAD reported no
speech at all for the first 30.4 s. Each keyterm outcome below is one sample on different speech. Nothing here is
evidence that keyterms help or hurt.

| | Run 1 (keyterms off) | Run 2 (keyterms on, 57 terms) |
|---|---|---|
| Call length | 59.9 s | 56.9 s |
| `speech_start` events / transcripts | 19 / 19 | 8 / 8 |
| Transcripts with text / empty | 13 / 6 | 7 / 1 |
| Frames dropped / resyncs / lost to Prisma | 0 / 0 / 0 | 0 / 0 / 0 |
| Filler (silent) frames | 3 (0.19 s) | 3 (0.19 s) |
| Real microphone frames sent | 912 (58.4 s) | 866 (55.4 s) |
| Speech seconds, our loudness check | 12.4 | 8.3 |
| Speech seconds, Prisma's `audio_duration_ms` sum (padding and empty segments included) | 37.2 | 18.4 |
| `processing` or `error` messages | 0 / 0 | 0 / 0 |
| First `speech_start` | 4.9 s | 30.8 s |
| Ledger estimate for the call | 0.279 | 0.138 |

The ledger figures are **our estimate** (Prisma-reported seconds at 0.45 per minute), not the dashboard. The empty
segments are inside that count; whether Gnani bills them is unknown. The dashboard deltas for these runs have not been
recorded.

**What the events prove**

- Prisma sent exactly one `transcript` per `speech_start`, in both runs (utterance ids match one to one). The page
  shows every transcript that has text and hides the empty ones, so **the page did not lose anything**: the digits and
  "AnyDesk" are in no `text` field of any transcript in either run.
- Our transport was clean: no frame dropped, no stall, none lost, 3 filler frames per call. The 912 + 3 frames are 58.6 s
  of a 59.9 s call. So missing words are **not** explained by our pacing, resampling backlog or the connection.
- Run 1, the number: after "मेरा नंबर है" (transcript at 18.0 s) Prisma's VAD fired one segment at 20.1 s that came back
  **empty** (1.65 s of audio, latency 9 ms), then reported **no speech for 8.3 s** (20.1 to 28.4 s), then one segment
  "सात चार एक" (1.94 s). A slowly read 10-digit number therefore reached us as at most two segments and three digit words.
- Empty segments return in 7 to 13 ms. Segments with text take 148 to 430 ms. So the empty ones were not recognised
  as speech at all (no recognition ran); this happened 6 times in 19 segments in run 1 and once in 8 in run 2.
- Keyterms: `keyterms` is in `content/keyterms.json` for all of "CBI", "AnyDesk" and "KYC" (also "ED"). Run 1's `start`
  event shows 0 keyterms given to the session and run 2's shows 57. `build_headers` adds the `keyterms` header only
  when terms are given (header names without it: 9; with it: 10; value 668 characters), and the test with the fake
  server reads that header off the wire. **Not proven from Prisma's side:** the `connected` message echoes no
  keyterms in either run, so we cannot see that Prisma applied them. (This build now logs the header names it sent.)

**What is a guess** (most likely first; none is proven)

1. *The digits were too quiet or too isolated for Prisma's VAD (`x-vad-threshold 0.7`, `x-min-speech-ms 200`).* A slow
   digit-by-digit reading is short, soft, isolated sounds with pauses between them; a speech-probability threshold of 0.7
   is a likely reason for 8.3 s with no `speech_start`. The 6 empty segments (VAD fired, nothing recognised) fit this too.
2. *The browser's own processing.* The page asks for noise suppression and automatic gain control (the Chrome defaults,
   written out on purpose). Both can attenuate or gate short isolated sounds. Echo cancellation should be harmless with
   headphones.
3. *The digits were not spoken audibly in the window*, or in run 2 the first 30 s were silent. Our loudness count (8.3 s
   voiced over the whole of run 2, against 16.9 s of text segments that include padding) leaves little room for a hidden
   30 s of normal-volume speech, but it is not timestamped, so this is an inference.
4. *"CBI" as "BBI" (run 1) and "EDBI" (run 2):* one sample each, different speech. In run 2 the list contains "ED", and
   the output began with "ED"; the list also has short terms (ED, DoT, PAN) that could be pulling words. This is a hunch
   and needs the same audio through both settings.
5. *"AnyDesk" never appeared in either run, with or without the keyterm.* It may have fallen into an empty segment or
   a gap, or been heard as something else; the logs cannot say.

**What would separate the guesses (no audio or VAD change made yet):**

- Free: run one call with `MIRAGE_SAVE_AUDIO=1` (saves the exact 8 kHz audio we send to `data/audio/`, deleted after 7
  days) and listen to or measure the digit stretch. If the digits are audible there, the VAD is the cause; if they are
  faint or missing, the browser path is.
- For a fair keyterms comparison: replay that one saved clip through Prisma twice, with and without keyterms
  (`scripts/smoke_prisma.py --keyterms ...`). Roughly 0.2 to 0.3 credits per run for a 30 to 40 s clip, estimated.
- Possible later experiments (not done, they change settings): browser noise suppression and gain control off, a lower
  `x-vad-threshold`.

**Bug fixed:** the page's end-of-call line read "Ledger: 18.79 of (0.0%)" because the `ended` message did not carry the
pool figures and the page printed the blanks. The message now includes `pool`, `pool_fraction` and `warning`, and the
page leaves the ledger line alone if the figures are missing. A test runs the page's own code in Node (skipped if Node
is missing) and fails when the old behaviour is put back.

**Logging added** so the next run can be checked without guessing: the names of the headers actually sent (never values)
and the whole raw transcript message from Prisma are now in the evidence log.

## Voice path, stop 1: the re-run with a proper microphone (2026-11-02)

**Finding for the README: capture quality dominates everything downstream.** The first two runs used a headphone
microphone and gave thin audio: Prisma's VAD reported no speech for long stretches (8.3 s mid-call in run 1, the first
30.4 s of run 2), 6 of 19 segments came back empty, and a slowly read number was lost. With a proper microphone the same
kind of script came through. The owner found the cause by swapping the microphone. We did not save audio from the thin
runs, so the cause rests on that swap and cannot be examined further. **The friend session needs a good microphone;
headphones are only for Uncle's voice.**

Script (owner): "Namaste, main Rajesh bol raha hoon. Mujhe CBI se phone aaya tha. Unhone kaha KYC update karna hai. Phir
mujhe AnyDesk download karne ko bola. Case number hai chaar saat do nau ek.", then "Phone number hai ... zero, zero, zero,
zero, do, saat, chaar, ek, nau, chhe" with a pause after each digit. Two calls, keyterms off (`20261102_181732_41ec`) and on
(`20261102_181944_fa72`).

- **Result:** CBI, KYC, AnyDesk and the case number (4 7 2 9 1) came through in both calls. The phone number arrived in
  pieces ("जीरो", "Zero", "zero zero दो सात चार एक नौ छह") that add up to 0000274196, and the owner then said the whole
  number once more ("zero zero zero zero दो सात चार एक नौ छह"). **One sample per run, not a measured rate; E1 will measure
  digit capture.**
- **Keyterms: no evidence either way**, one sample each with nearly identical text. They stay OFF by default (the server's
  default is now `off`); test them later on one saved clip replayed with and without.
- **Fluent speech arrives late.** About 15 s of continuous reading came back as one transcript at 16.6 s (13.3 s of audio,
  run 1) and 17.2 s (16.2 s of audio, run 2): Prisma waits for 500 ms of silence, so a long stretch is one late
  transcript. A real scammer talks in long stretches. On the later list: a larger `x-max-speech-s` as a safety cap (about
  20 to 30 s). Caveat: it cuts at a time limit, so it can split sentences and numbers (we saw this with 6 s).
- **The ledger is an estimate that leans high.** The two runs moved it 0.234 and 0.237 credits (35 s calls), counting
  Prisma's reported seconds including padding and empty segments. The dashboard figure for these runs is still to come; then
  `python -m mirage.credits sync` and the log will say by how much it leans.

### Offline replay of those two calls through the brain (no network, no audio)

Transcript text and arrival times only, the real brain and gate, synthetic mode on, the 1.8 s digit-mode wait, a reply
firing when the wait passes with nothing newer. Script: scratchpad `replay_calls.py`, not in the repo.

- **The phone number does come out: `0000274196`** (valid, "keyword" confidence, 00-prefix synthetic), in both calls,
  and the case number `47291`. **But only from the final repeat**, not from the pieces.
- **The pieces were not joined.** The first transcript ended "...Phone number है जीरो" and "Zero" followed 1 s later
  (joined correctly). The brain then answered at t = 19.4 s (call 1) / 20.0 s (call 2), while the caller was still
  reading digits; the next piece ("zero zero दो सात चार एक नौ छह", 7.9 s of audio) only arrived at 26.1 s / 26.3 s. It was
  extracted alone as an unlabelled `ambiguous 00274196` and stayed in the stored intel beside the phone number. The brain's
  reply at 19.4 s ("Woh app download hi nahin ho raha...") talked over a caller who was mid-number.
- **Cause:** a transcript arrives only after the utterance ends (500 ms of silence), so "no transcript for 1.8 s" does not
  mean the caller stopped. The speech-time join in `extract_intel` is fine: the pieces start 0.6 s apart in speech time, and
  fed together they give `phone 0000274196` (checked). The loss is in the turn-taking: the reply fired before the later
  pieces arrived.
- **What-if, not implemented:** holding the reply while Prisma has sent a `speech_start` with no transcript yet. In both
  calls `speech_start` for the next piece arrived 0.11 s to 0.22 s after the previous transcript, well inside the 1.8 s
  wait, so a hold would have caught it. With the hold, the whole call is one turn and the stored intel is clean (case and
  phone once each). Two samples only.
- **The repeat is not counted as a different number:** stored intel keeps one `phone` entry (entries are de-duplicated on
  kind and value). Two loose ends: in the live-style replay the `ambiguous` fragment stays as its own entry; and when the
  first full number and its repeat land in the same turn, that turn's `findings` list holds the number twice (same value).
  Neither was changed. To decide for stop 3: hold the reply while an utterance is open (with a ceiling, so a stuck
  utterance cannot block forever), de-duplicate findings within a turn, and drop an `ambiguous` fragment once a labelled
  number contains it.

## Voice path, stop 2 (text to Timbre to the speaker)

Built and tested against an in-process mock of Timbre. **Nothing has run against the real service yet.** No audio was
rendered, no line was pre-rendered, and no real 429 was provoked (the 429 behaviour is tested with a mock only).

- **Docs re-read** (docs.gnani.ai/api/TTS/tts-sse.md): the request goes to `/api/v1/tts/sse` with the key in
  `X-API-Key-ID`; the reply is Server-Sent Events (`start`, `chunk` with base64 audio, then `complete`, or `error`).
  **One active request at a time:** a second gets HTTP 429 with `RATE_LIMITED` and the text "Only 1 active request is
  allowed at a time". 403 means key, organisation or credits; 500 and 503 are temporary. **The page does not state**
  a character limit per request, any retry or backoff advice, or anything about billing. So `tts_max_chars` (400),
  the retry waits and the deadline are our guesses, and the 429 reading is only as good as that one documented example.
  The docs also tell you to write numbers "as spoken words"; we keep the spaced numerals chosen by listening.
- **Built:** `mirage/timbre.py` (client, cache, one-at-a-time queue, `Speaker`), `mirage/voicetest.py` (the fixed line),
  `mirage/gaterequired.py` (the gate-required rule, now shared), the `/ws/speak` route, and the "Speak the test line"
  button with a small player in the page. Settings are in `VoiceConfig` (guesses, except the voice, language and 24 kHz
  rate, which were chosen by listening).
- **429 and temporary errors:** a 429 or a 5xx before any audio is retried twice, after 0.5 s and then 1.0 s, inside a
  10 s deadline; a `Retry-After` is honoured when it fits (a 30 s one gives up at once, since a line that late is
  worthless). A 429 that reads as out of credits is not retried. A failure after audio has started is not retried
  (that would speak the start of the line twice). When it gives up, the line is NOT spoken and the page says so.
  **The wording that tells "busy" from "out of credits" is a guess** apart from the documented "active request"
  sentence. The first 429 body ever seen is saved once, with the key blanked, in `data/timbre_first_429.txt`.
- **One at a time, newest wins:** the queue runs one request; a newer line replaces one still waiting (its caller gets
  nothing back); the line already being spoken is never cut off. Tested with a mock that records how many requests
  ran at once (never more than 1).
- **Cache:** in memory only, bounded (64 lines, 20 MB), least recently used out. The key includes text, voice, language,
  speed and rate. A repeated line is served from memory in about 0.2 s pieces, costs nothing and sends no request.
- **Billing in our ledger:** a line is counted (characters, tags included, at 2.7 per 1,000) when its first audio arrives;
  a request that fails with no audio is not charged. Unknown: whether Gnani bills failed or partial requests the same way.
- **The gate on the speaking path:** `Speaker` is built with the gate (no gate, no Speaker) and refuses any plan that
  did not come back from the gate. To make that checkable, **every plan the gate returns is now stamped `gated=True`**,
  including one it passes unchanged (before, a clean pass left no mark on the plan, only an entry in the gate log). The
  stamp guards against forgetting the gate, not against forgery. The test button's line is the brain's first reply to
  "Hello?" (a template line), run through the gate; **the browser never sends the text**, and a test sends a hostile
  `text` field to prove it is ignored.
- **Tests:** 36 new (529 in all). Nine deliberate breakages were each caught: skipping the `gated` check, skipping the cache,
  no retry waits, removing "newest wins", skipping `require_gate` in the speaker, removing the origin check on the new route,
  charging before audio arrives, not stamping gate output, and not counting a gap in the page's player. One of them
  (removing "newest wins") first made a test hang instead of fail; the test now has a 5 s limit.
- **Not built (stop 3 and later):** the brain loop, half-duplex muting, pausing replies while a caller's utterance is open,
  playback-finished reporting back to the server, the dashboard's stage and intel panels, latency percentiles.
- **Unknown until the first real run:** time to first audio, whether `<coughs>` style tags in the text come through as
  intended on the 24 kHz raw stream, whether the service sends a WAV header despite `raw` (handled if it does), whether the
  browser plays chunks without audible gaps, and what Timbre really charges for the 49-character test line.

## Turn-taking: the hold rule, and tidying repeated numbers (offline, no network)

Built after the replay of the two good-microphone calls showed Uncle answering mid-number. All offline.

- **The hold rule** (`mirage/turns.py`, `TurnScheduler`). A reply is held while Prisma has an utterance open (a
  `speech_start` with no transcript yet). Any transcript for that utterance closes it, **empty ones too**, so noise blips
  cannot freeze Uncle (6 of 19 segments were empty in the thin-mic run). An utterance still open after
  `FSMConfig.hold_ceiling_s` (**10 s, a guess**) is treated as stuck and stops blocking. Otherwise the reply is due when the
  brain's own wait (0 s normally, 1.8 s in digit mode) has passed since the newest transcript. It has no clock and no
  network: the time is passed in, so it can be tested and replayed.
- **What Uncle does when the ceiling fires:** he answers what has been heard so far, as if no utterance were open
  (a `hold_ceiling` entry is logged). If that utterance's transcript turns up later it is logged as a late transcript and heard
  as a new turn. The ceiling is not a recovery for a lost utterance, only a way never to be silent forever.
- **Tidying:** the same kind and value found twice in one turn is kept once (at its first mention), and an unlabelled digit run
  ("ambiguous") that is a piece of, or equal to, a longer or labelled number is dropped, both within a turn (`extract_intel`)
  and in the stored intel across turns (`CallBrain._record_intel`). A different number is never merged. One identical
  value under two different kinds (phone and account) is two findings.
- **Replay tool:** `python -m mirage.replay <call folder or fixture> [--normal-wait S] [--ceiling S] [--drop-last N]`.
  The two good calls are saved as fixtures in `tests/fixtures/calls/` (transcript text, arrival times and utterance ids
  only, from the owner's staged script; no audio, no key, no request ids).

**Replay results (the real calls, keyterms off / on):**

| | Before | After |
|---|---|---|
| Uncle's replies in the call | 3 turns; the 2nd at 19.4 s, mid-number | 2 turns (3.05 s greeting, then 33.6 s after the whole dictation), none during the number |
| Phone number | only from the final repeat; the pieces left an unlabelled `ambiguous 00274196` | `0000274196`, keyword confidence, built from "जीरो \| Zero \| zero zero दो सात चार एक नौ छह" |
| Findings in the turn | the number twice | once |
| Stored intel | case, phone, and a stray fragment | case `47291` and phone `0000274196` |
| **Without the final repeat** (`--drop-last 1`) | the number never labelled | `0000274196`, labelled `phone`, both calls |

A test switches the hold off (ceiling 0) and shows the old failure coming back, so the replay tests can fail.

**Trade-offs found by the replay (not fixed; for decision):**

1. **The ceiling can fire on genuine speech.** In the recorded call one real utterance stayed open for 13.5 s (the opening
   monologue). A 10 s ceiling releases it mid-speech (Uncle says "Haan ji boliye" at 13.05 s while the caller is still talking);
   15 s does not. Longer ceilings mean longer worst-case silence for a truly stuck utterance.
2. **A chain of utterances is never "stuck".** With a 15 s ceiling the whole 30 s monologue was answered once, at 33.6 s,
   because each utterance started right after the previous transcript. There is no cap on the total hold.
3. **With the normal wait at 0 s the hold cannot see the next utterance.** The greeting went out at 3.05 s, in the same
   millisecond that the next `speech_start` arrived (in 4 of 4 samples the next start came 0 to 0.2 s after the previous
   transcript). A normal wait of 0.3 s catches it, at the cost of 0.3 s on every turn. Not changed.
4. Only two calls, one speaker, one microphone; the numbers are what happened, not rates.

## Later list

- Spelled-letter patterns like "M for Mohan" (skipped for now; STT letters are the weak point, E1 will measure it).
- FSM, digit-dictation state: wait a longer, configurable silence (start around 1.5 to 2 s) before replying, and
  join transcripts that arrive close together (`join_window_s` in `extract_intel`, 2.5 s to start).
- `safety.py`: base the output gate on where digits came from (Uncle's fictional fact sheet or the caller's own
  transcript, via a Finding's quote), not only on patterns, because Uncle must be able to read back the caller's digits.
  `looks_like_real_mobile` is only a pattern check on whole digit runs.
- Client-side end-of-speech detector with `commit` as an optimisation, with its own evaluation.
- A larger `x-max-speech-s` (about 20 to 30 s) as a safety cap only. It cuts at a time limit, so it can split sentences and numbers.
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
