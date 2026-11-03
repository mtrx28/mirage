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

## What Uncle does when the caller does not look like a scammer

- Uncle always tells the truth about what he is. If someone sincerely asks "are you a robot?", and the call shows little sign of a
  scam, he says so at once: **"Main ek automated assistant hoon, insaan nahin."** If the call shows strong signs of a scam he first
  evades twice in character (he "cannot hear"), then answers honestly. He never claims to be a person.
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

## Results

The numbers so far (digit capture, latency, red-team, friend role-play) are in [`docs/results.md`](docs/results.md), each with how many calls or lines
it rests on, what kind of data it is, and what it does not show. All of it is development data from staged calls; E1 digit fidelity has too few samples
to quote and is not run yet.

## What we do not know

We have **not** taken legal advice. We do not know whether, where or when a law requires an automated caller to say that it is one,
whether recording or transcribing a caller needs notice or consent, or whether a synthetic voice must be labelled. The behaviour above
is the project's own policy, not a statement that it is lawful in any place. See `docs/safety.md` for the scope (only four scam types
are covered), the scam score, and the measured limits.
