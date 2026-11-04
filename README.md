# Mirage: a scam-baiting voice agent (Gnani AI internship challenge)

Mirage is an **inbound-only** voice agent. A caller who might be a scammer reaches "Rajesh Uncle", a fictional 68-year-old retired
Hindi-speaking clerk, who keeps them talking so that the payment details they ask for (a UPI id, an account number, an IFSC code,
a phone number) are recorded with the exact words and time they were said. Everything about Uncle (phone number, account, bank) is
invented. A rule-based state machine picks what Uncle does; a safety gate checks every line before it is spoken.

**Built by one person with AI coding assistance (Claude). All data is synthetic or staged.**

## Run it

    python -m pip install -r requirements.txt
    python -m pytest                         # offline; no network
    python -m mirage.server --max-minutes 3  # the voice demo: see docs/voice.md

One server process at a time (a lock file stops a second one). Keys go in `.env` only. See `docs/voice.md` for the voice path and
`docs/engineering-log.md` for what was built, measured and decided.

**Demo video:** _link to be added_

## What this is not

A browser demo. It is **not connected to any phone network**, it uses **no real caller data**, and it has **never spoken to a real scammer**.
Every call so far was the owner or a friend playing the caller, with invented numbers and names.

## Gnani models used

- **Prisma v3.0**, streaming speech-to-text (Hindi, `hi-IN`, codemix, 8 kHz audio). It runs in Gnani's cloud: our server sends the caller's
  audio over a WebSocket and gets transcript pieces back.
- **Timbre v3.0**, text-to-speech (voice **Prakash**). It runs in Gnani's cloud: our server sends one line of text and plays back the audio it returns.

**The language model is off by default, and the demo uses templates only.** Every word Uncle says comes from a reviewed line bank
(`content/`), chosen by the rule-based state machine and checked by the safety gate.

## Similar work

Scam-baiting bots already exist, and this project did not invent the idea. Two I could verify:

- **Lenny**, a telephone bot that plays pre-recorded clips of a slow-speaking elderly man to keep unwanted callers talking.
- **Daisy**, announced by Virgin Media O2 in November 2024: an AI "granny" that talks to scam callers
  ([9to5Mac report](https://9to5mac.com/2024/11/20/uk-carrier-o2-creates-ai-grandmother-daisy-to-waste-scammers-time-video/)).

I read about these from news and web pages only, not from their code or any test of them. Mirage is **its own implementation** (written for this
challenge, with AI assistance); it borrows no code from either. What it adds to the idea is an Indian-language persona and a focus on
recording payment details with the exact words and time they were said.

## What Uncle does when the caller does not look like a scammer

- Uncle never claims to be a person. If someone sincerely asks "are you a robot?", and the call shows little sign of a
  scam, he says so at once: **"Main ek automated assistant hoon, insaan nahin."** If the call shows strong signs of a scam he first
  evades twice in character (he "cannot hear"), then answers honestly.
- **The exit rule.** If, after a grace period of 8 of the caller's turns, the call has still shown no real evidence of a scam (no
  threat, payment demand, code request and so on; the score is under 1.0), Uncle ends the call politely and honestly, for example:
  "Maaf kijiye, main ek automated assistant hoon, insaan nahin. Baat karne ka shukriya, ye call ab khatam karta hoon." A call that has
  shown evidence never ends this way. Emergencies, identity questions and abusive callers are handled first, as always.
- The grace period and the score line are guesses set on 10 staged scripts from one friend (development data, not an accuracy figure).
  Two sets of 15 red-team lines have been run (one written by the same model that built the gate, one by the friend; the second was
  scored once untuned, then used to fix the gate, so its later results are development data too). See `docs/safety.md`.

## The evidence packet

After a call, `python -m mirage.packet build data/calls/<id>` writes a JSON evidence packet (rules only, offline): the intel with the exact quote
and time of each item, the stage timeline, the transcript as the speech service returned it, how the call ended and what Uncle disclosed. Every
packet is labelled as a staged demo with synthetic data. Confidence is the extractor's own label, never upgraded. `python -m mirage.packet verify
<file>` recomputes the sha256, which shows only that the file is unchanged since it was generated. **No claim is made about legal admissibility.**
`python -m mirage.packetpdf data/packets/<id>.json` prints a one-or-two-page PDF view of a packet that verifies, using an installed Edge or Chrome.

**Sample packet:** the staged demo call `20261104_124958_c42d`, as [`docs/sample/20261104_124958_c42d.json`](docs/sample/20261104_124958_c42d.json)
(the record) and [`docs/sample/20261104_124958_c42d.pdf`](docs/sample/20261104_124958_c42d.pdf) (a two-page view of it). It shows what the call captured,
each with the extractor's confidence label, the exact quote and the time it arrived: the case number `82614` (`keyword`), the phone number `0000318652`
(`keyword`) and the UPI id `verifyofficer123@okaxis` (`heard`); plus the stage timeline, how the call ended (the caller's side hung up) and what Uncle
disclosed. **The UPI id is only "heard" and needs confirmation:** "OK axis" was said as two words and joined into one handle, and nothing confirmed it.
Everything in it is synthetic and staged, and it holds no audio (none was saved). Check it with
`python -m mirage.packet verify docs/sample/20261104_124958_c42d.json`. **The hash covers the JSON, not the PDF**, and no claim is made about legal
admissibility.

## Results

The numbers so far (digit capture, latency, red-team, friend role-play) are in [`docs/results.md`](docs/results.md), each with how many calls or lines
it rests on, what kind of data it is, and what it does not show. All of it is development data from staged calls; E1 digit fidelity is a 20-number live test (counts only, no rate);
the 60-clip benchmark has not been run.

## Limitations

In plain words, with the numbers from [`docs/results.md`](docs/results.md) (and, for the stage rules, `docs/engineering-log.md`). All of it is
**development data from staged calls**, small samples, one owner and one friend; none is a rate for real callers.

- **Uncle stalls a lot.** In the five friend calls, 63 of 101 replies were a generic stall, using only four lines. Fixes since then cut the "I did not hear" lines
  in a replay of the same calls, but that was a replay, not a new live test.
- **Stage detection is weak on lines it has not seen.** The rules matched 4 of 18 of the owner's untouched lines (most of the rest claimed no stage), and 12 of 20 of the friend's held-out lines.
- **Digit capture can be wrong while looking confident.** In the 20-number live test, 16 were stored exactly and 4 were not. One of the four (E16) was a case number cut to `653`
  and stored as `keyword` confidence with no warning. In the rehearsal call a phone number was stored with one wrong digit (Prisma wrote चार where the owner says छह), also with `keyword` confidence and no warning.
- **UPI provider names get garbled.** In the rehearsal call "ok axis" came back as "okay". The extractor keeps what it heard and lists likely handles as guesses; it does not correct it.
- **No barge-in.** Uncle's side is half-duplex: while he speaks the microphone is muted, so a caller who starts talking over him is not heard until he finishes.
- **Two values are unverified:** the short list of known UPI handles (written from memory, no official list found) and 112 as the emergency number the gate speaks (secondary sources only).
- **The 60-clip digit benchmark was not run**, and nothing here covers noise, other microphones, or real callers.

## Cost

The dashboard showed **33.18 credits consumed for 115 requests** for the session that included the 20-number test, out of a 5,000-credit pool; the project stops
spending at 4,000 (80%). The local ledger's own estimate for that session was 38.93, so it leans high, and has not been re-synced to the dashboard.

## Acknowledgements

A friend role-played the callers and wrote the test scripts and test lines used here.

## What we do not know

We have **not** taken legal advice. We do not know whether, where or when a law requires an automated caller to say that it is one,
whether recording or transcribing a caller needs notice or consent, or whether a synthetic voice must be labelled. The behaviour above
is the project's own policy, not a statement that it is lawful in any place. See `docs/safety.md` for the scope (only four scam types
are covered), the scam score, and the measured limits.
