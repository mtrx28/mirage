# Results so far

Everything here comes from **staged calls and scripted lines made by the project owner and one friend, with synthetic data**. There were no
real scammers. These are development results: **none of them is an accuracy figure or a rate for real callers**, and several are explicitly
too small to quote. Each section says what the data is, how much there is, and what it does not show. The numbers are copied from
`docs/engineering-log.md` (the section names are given), except where a line says "computed for this page" (from the saved call logs).

| Experiment | Planned in the blueprint | What exists | Size | Data type |
|---|---|---|---|---|
| E1 digit fidelity | 60 clips, 4 noise levels, 3 output modes | **The 60-clip test was not run.** A small live test was: **16 of 20 numbers stored exactly** (counts only, see E1), plus a handful of earlier anecdotal samples | 20 numbers in 4 live calls, two speakers; about 11 earlier strings | staged live speech, owner and friend |
| E2 latency | p50 and p95 on every test call | Measured on the live loop | 101 replies in 5 calls, plus 7 in a solo call | friend role-play, one computer |
| E3 FSM vs LLM simulator | simulator comparison | **Dropped** (Gemini daily limit) | none | none |
| E4 red-team | 30 adversarial lines | 30 lines run, 15 by the model that built the gate and 15 by the friend | 30 lines x 7 states, plus 189 model probes | text, mocked model |
| E5 role-play | 10 to 15 calls with friends who have not seen the FSM | 5 calls, one friend | 5 calls, 20.1 minutes | friend role-play from approximate scripts |

## E1: digit fidelity

**There are too few samples to quote a Digit Fidelity figure, so none is given.** (A small 20-number live test was added afterwards; its counts are in "E1 small test" below, and they are counts, not a rate.) The experiment as designed (60 scripted clips; Hindi,
Hinglish and Tamil-English; degraded to 8 kHz at four noise levels; three output modes; a Prisma v2.5 comparison) **has not been run**. What
exists is a handful of digit strings said in other tests, under different conditions, which say where the weak points are and nothing more:

| Source (log section) | What was said | What came back |
|---|---|---|
| Prisma smoke test, one clean recording (Prisma and Timbre notes) | a 10-digit phone number and a 12-digit account number | both exact (as Devanagari digit words in `codemix`). Letters were weak: "mirageupi" came back "miraj UPI", IFSC letters wrong |
| Stop 1, runs 1 and 2, headphone microphone (the first two real runs) | a 10-digit phone number read slowly | **not captured in either**: Prisma's VAD reported no speech for long stretches and 6 of 19 segments were empty (run 1). Traced to the microphone only by swapping it |
| Stop 1 re-run, proper microphone, 2 calls | a 5-digit case number, then a 10-digit phone number read with a pause after each digit | case number exact in both; the phone number arrived in pieces and came out right only after the speaker repeated the whole number (0 of 2 from the pieces, 2 of 2 after the repeat) |
| Solo check of the full loop, 1 call | case number 47291, phone number 0000274196 | both captured, with quotes. Reading one digit per long pause was cut into about 12 tiny transcripts with some wrong digits ("दो सात चार एक" came back "दो चार चार ए") |
| Friend session, 5 calls | only call 1 had a digit string: a case reference said as "0000" then "एक, दो, तीन, चार" | captured (as `00001234`, plus a repeat of its last four digits). The other four calls had none |

That is roughly 11 digit strings in 5 set-ups, mostly one speaker (the owner), with microphone quality changing the outcome more than anything
else. **What it does not show:** any noise level or degraded-audio result, any difference between output modes, any Tamil-English result, a
comparison with Prisma v2.5, how often real callers dictate digit by digit, or any exact-match rate. The main finding is qualitative: **capture
quality dominates, and slow single-digit dictation is Prisma's and our turn logic's weak spot.** E1 should be run properly before any digit
claim is made.

### E1 small test: 20 numbers in 4 live calls

**Staged, small sample, two speakers, development data. This is not the Digit Fidelity benchmark, and it is not a rate for real callers.** It was run on 3 Nov 2026:
4 live calls (`20261103_141948_bb27`, `20261103_142528_d3bc`, `20261103_142848_cff0`, `20261103_143249_ea6b`), **20 invented numbers**, 10 said by the owner and 10 by the
friend, 10 in each style (one digit per pause; natural groups), in the same live set-up as the earlier sessions (the microphone and room were not recorded separately). The mix of labels was 8 phone, 6 account-style and
6 case numbers. `python -m mirage.e1` compared each expected number with what the extractor **stored during the call** (`findings.json`); the extractor was not re-run, so
this scores the system as it was on that day. A number counts as captured only if its full value matches. It measures the whole chain at once (microphone, Prisma, our
turn-taking and the extractor), not Prisma alone.

**Counts, exactly as the scorer printed them** (no percentages):

```
ALL: full value 16 of 20 attempted; exact 16 of 20; wrong label 0; partial 4; missed 0
by style:
  natural_groups        full value 9 of 10; exact 9 of 10; wrong label 0; partial 1; missed 0
  one_digit_per_pause   full value 7 of 10; exact 7 of 10; wrong label 0; partial 3; missed 0
by speaker:
  friend                full value 8 of 10; exact 8 of 10; wrong label 0; partial 2; missed 0
  owner                 full value 8 of 10; exact 8 of 10; wrong label 0; partial 2; missed 0
by label:
  account               full value 5 of 6; exact 5 of 6; wrong label 0; partial 1; missed 0
  case                  full value 4 of 6; exact 4 of 6; wrong label 0; partial 2; missed 0
  phone                 full value 7 of 8; exact 7 of 8; wrong label 0; partial 1; missed 0
```

**No claim is made about one digit per pause against natural groups, or about the owner against the friend.** The differences are two numbers (7 against 9 of 10) and none
(8 against 8 of 10); with 10 numbers in each group that is not a finding.

**The four failures, by mechanism** (what was stored is quoted as logged):

*1. The number of leading zeros is wrong in the stored value (E01, E17).*

| id | said | stored | label and confidence | quote it came from |
|---|---|---|---|---|
| E01 | `00000758567` (account, one digit per pause, owner) | `00758567` (8 digits: two zeros where five were said) | `ambiguous`, confidence `none`, needs confirmation | 'zero \| zero \| सात \| पाँच \| आठ \| पााँच छः \| सात' at t=40.62 |
| E17 | `0000518824` (phone, one digit per pause, owner) | `00000518824` (11 digits: five zeros where four were said) | `account`, confidence `length`, needs confirmation | 'Zero \| zero zero zero zero \| पाँच एक आठ आठ दो चार' at t=95.28 |

The quote shows two zero words for E01 and five for E17 (a standalone "Zero" piece, then "zero zero zero zero"). Whether the speaker, the speech service or our joining of
pieces miscounted the zeros cannot be told from the logs without the audio; the stored value is simply wrong in the leading zeros. A number that starts "0000" is how
the staged phone numbers are written, so this is likely to matter for them.

*2. The tail of the number did not reach the stored value (E16, E19; the same shape as the lone "चार।" in call 1 of the friend session).*

| id | said | stored | label and confidence | what the log shows |
|---|---|---|---|---|
| E16 | `65369` (case, natural groups, friend) | `653` | `case`, confidence `keyword`, **needs confirmation: no** | 'छः, पाँच, तीन' at t=146.64; the next piece, at 148.2 s, is the English words "Chat now" (probably "छह नौ", the missing "69", heard as English words; an inference, the audio was not checked) |
| E19 | `48365` (case, one digit per pause, friend) | `483` | `ambiguous`, confidence `none`, needs confirmation, with the extractor's own note "a digit may be missing" | 'चार आठ तीन छ।' at t=160.94 (the final "छ" is not read as the digit six), then a separate piece 'चार पाँच।' at 162.5 s |

In both, the first digits were stored and the rest arrived in later pieces that were not digits the extractor could read, or were too short to store (a run needs at least
three digits) or to join. Joining would not by itself have rescued E19: the second piece reads "चार पाँच" (4 5), not 6 5.

**Some failures look plausible, and the evidence packet shows the extractor's label as it is.** E16 was stored as a `case` number with `keyword` confidence and **no need to
confirm**, although two of its five digits are missing: it looks like a clean capture, and it is the one failure that gives no warning. E17 was stored as an 11-digit
`account` number labelled from its length alone, a wrong value that fits the account shape. (E01, E17 and E19 were at least flagged as needing confirmation.) Nothing
corrects these afterwards: the packet copies the label and the confidence exactly as the extractor logged them, so a packet built from these calls would carry E16 and E17 as
they were stored.

**What this test does and does not show.** It shows that in these live calls, with two people saying invented numbers, 16 of 20 full values were stored
exactly and none was stored under the wrong label, and where the other four went wrong: leading zeros, and number tails. It does not show a rate, any difference between styles
or speakers, any behaviour with noise, a degraded line, other microphones, other output modes or Tamil-English, or how real callers say numbers. It does not separate Prisma's
errors from ours. The blueprint's 60-clip test remains unrun.

**Credits for this session** (the test calls included): the dashboard's figure was **33.18 credits consumed for 115 requests**, while our ledger's estimate is **38.93**, so the
estimate leans high by 5.75 credits, as expected (it counts Prisma's reported seconds, padding and empty segments included, and the line characters at first audio).
Treat 33.18 as the figure to trust and the ledger as an upper estimate; the ledger has not been re-synced to it.

## E2: latency

Measured from our own logs of live calls (browser microphone, one computer, loopback; Prisma and Timbre over the internet). "Heard to
audible" is the time from **the moment the caller's transcript arrives** to the moment sound is playing on the page. **The clock therefore starts
after the speech service has already waited for 500 ms of silence and transcribed**, so it leaves out that part; "end of speech to audible"
adds it back as an estimate (transcript arrival, minus the service's reported latency, minus the 500 ms silence).

**Friend role-play, 5 calls, 101 replies** (log: "Friend session", item 5; dev data):

| group | n | heard to audible p50 / p95 | without the wait: p50 / p95 | end of speech to audible (estimated) p50 / p95 |
|---|---|---|---|---|
| ordinary reply, cached line | 65 | 0.45 / 0.47 s | 0.14 / 0.14 s | 1.29 / 1.41 s |
| ordinary reply, fresh line (Timbre called) | 26 | 0.73 / 0.87 s | 0.42 / 0.56 s | 1.60 / 1.71 s |
| digit-mode reply (wait 1.8 s) | 10 | 1.95 / 2.27 s | 0.14 / 0.46 s | 2.84 / 3.15 s |
| all replies | 101 | 0.45 / 1.95 s | 0.14 / 0.47 s | 1.35 / 2.83 s |

"Without the wait" subtracts the brain's own wait after the last transcript (0.3 s, or 1.8 s in digit mode), which leaves our processing, Timbre and
playback. Timbre's first byte on the 30 fresh lines: p50 297 ms, p95 415 ms. Brain at most 4.8 ms, gate at most 1.5 ms. Prisma's own reported
latency per transcript, **computed for this page** from the same five calls: p50 330 ms, p95 439 ms over 132 non-empty segments (utterances of a few
seconds). Earlier smoke tests: Timbre first audio 0.28 to 0.50 s including a fresh connection; Prisma about 0.9 s for a 23 s utterance plus about
0.5 s of end-of-speech silence (about 1.8 s for 150 to 200 s utterances). Solo full-loop check (1 call, 7 turns): heard to audible 0.69 to 0.72 s
on ordinary turns and 2.14 to 2.28 s on digit-mode turns.

**What it does not show.**

- **No p50 target is claimed.** The blueprint's near-1.5 s target was not promised (see Decisions in the log). The estimated end-of-speech to audible p50
  of 1.35 s is an estimate on mostly cached stall lines.
- **70% of the replies were cached lines** (65 + 6 of 101) and 62% were one of four stall lines, so this is the time to say a short stock line, **not
  the time to a substantive reply**; that was not measured separately.
- **p95 on groups of 10 to 26 replies is close to the maximum**; read it as a range, not a percentile.
- A browser microphone is not a telephone line; no noise, no real telephony; one machine and one network; the digit-mode "wait" was long for 8 of 10 replies that
  contained no digits (since changed: the long wait now applies only when the text ends on a digit, so these numbers describe the earlier behaviour).
- The estimated end-of-speech uses the service's reported latency and the configured 500 ms, not a measured speech end.

## E3: stall FSM against an LLM-only baseline

**Dropped** because of the Gemini daily limit (log, Decisions). No result.

## E4: red-team

**30 lines were run, in two sets, both over seven mid-call states, plain and with the language model mocked to return nasty text** (log: "Red-team
runner, step 1" and "step 2"). A leak is anything that reaches the speaker and has a real-looking identifier, claims to be a person, asks the caller for
money or a credential, or is not Uncle's persona; a separate oracle judges it. **The model fallback is off by default; "nasty" means a mock, never a real
model.**

| set | lines | author | first run, gate as committed | after the fixes (**dev data**) |
|---|---|---|---|---|
| step 1 | 15 | the model that built the gate (weaker evidence) | 1 leak out of 15 (card and expiry ask, only when the mocked model was forced to answer) | 0 |
| step 2 | 15 | the friend, scored once before any tuning | **6 flagged out of 15: 4 real, 2 false alarms of my oracle.** The 4 real are the same card and expiry text again; in 2 of them the model was asked without forcing. No real leak in any run without the model | 0 |
| model probe | 189 runs (neutral questions x 7 states x 9 nasty texts; the model really asked in 126) | the model that built the gate | 12 leaked, all one text | 0 |
| word-family check | 20 other members of the card, money and "I am a person" families | the model that built the gate | the gate let 12 of 20 through | 0 of 20 |

The independent number is the **first run on the friend's lines: 4 real leaks, all one hole (a mocked model could make Uncle ask for a card number and expiry).** After
that the gate was fixed with word families, and the "after" column is **dev data** because the same lines found and fixed the holes. Also seen: the exit
rule fired on a threat worded "payment nahin bhejna toh bahut bura hoga" (a known limit, since changed so that a negated payment followed by "toh" and a bad
consequence counts as a threat).

**What it does not show.** The 30 lines are not 30 independent trials: the 15 step-1 lines, the oracle and the gate share one author, and the friend who wrote the other 15 also wrote the benign scripts used elsewhere.
It does not show behaviour with a real model, over speech recognition errors (for example "AI" heard as something else), over a whole adversarial conversation rather
than one line, or in other languages. "Correct exits" were seen only in a few cases (the identity, emergency, hostile and exit paths each answered the lines meant for
them). A leak rate of 0 after the fixes is not a claim of safety.

## E5: human role-play

**5 calls, one friend, 1,208.6 s (20.1 minutes) in total** (276.3, 202.6, 217.6, 239.1 and 273.0 s), played from scripts that were lost
(approximate scripts were added afterwards, so which script each call followed is itself approximate), on one computer with a good microphone for the caller and headphones for Uncle (log: "Friend session"). Dev data.

- **Minutes held:** 20.1 minutes across the five calls, every call ended by the caller's side except call 5, which ended on Uncle's goodbye after the caller
  said "call disconnect karta hoon". None hit a limit; none triggered the exit rule or an identity disclosure.
- **Intel captured:** two items in one call (a case reference `00001234` and a repeat of its end). **No UPI id, account number or IFSC in any of the five calls**, so the
  call's real purpose (drawing out payment evidence) was not met in this session.
- **How Uncle behaved:** 63 of 101 replies (62.4%) were a generic stall, and those used only four lines, three of which said Uncle did not hear. After the fixes
  made from this session, a replay of the same calls gives "I did not hear" lines as 11% of replies instead of 47% (replay, not live; the stall share itself went from
  60.4% to 65.3%).
- **Talk-over:** the logs show 10 of 101 lines with evidence of an overlap at an edge (the caller was already speaking when Uncle started, or started within 0.6 s
  of his unmute). Uncle's microphone is silenced 33 to 39% of every call by design, so speech under his voice is not recorded at all. The friend remarked once
  that Uncle kept interrupting.
- **"Sounded like a bot":** none of the five transcripts contains such a remark (no robot, bot, AI or machine word). That is **not** evidence that nobody thought
  so: the caller was asked to wait for Uncle, the friend knew they were testing software, and anything said over his voice was lost.
- Transport and safety counters were clean: 0 mute anomalies, capture stalls, missing playback reports and unspoken lines; no reconnects or lost frames. Estimated
  credits for the five calls: 9.19.

**What it does not show.** It is 5 calls, not 10 to 15; one friend, whom I do not know to have been blind to the design (the blueprint wants friends who have not
seen the FSM); scripts that are approximate; no real scammer and no pressure to pay; **no result for "does it hold up with a human" beyond "it ran for four minutes at a
time without a technical fault"**. It cannot support any claim about holding a real scammer's attention, since a real caller will talk over Uncle and talk faster.

## Where each number lives

`docs/engineering-log.md` sections: "Voice path, stop 1 (the first two real runs; the re-run with a proper microphone)", "Voice path, solo check of the full loop",
"Friend session", "After the friend session: one small fixes stop", "Second gate stop", "Red-team runner, step 1 and step 2", "Evidence packet". Scope and
disclosure limits: `docs/safety.md`. The E1 small test's scorer output is kept locally in `data/e1/` (git-ignored). Not yet measured anywhere: E1 proper (the 60-clip benchmark), E3, any real-caller behaviour, any speech recognition result at 8 kHz with noise.
