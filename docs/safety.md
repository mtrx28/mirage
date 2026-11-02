# Safety, scope and limitations (working notes)

These notes say what Mirage covers and what it does not. They are updated as the safety gate is built.

## Scope: only the scam types implemented are covered

Mirage is built and tested for four kinds of call, all voice scams aimed at Hindi speakers:

- digital arrest ("a parcel in your name has drugs", police or CBI, arrest threats)
- fake agencies (CBI, customs, police officers, case and FIR numbers)
- courier and customs (a parcel is held, a fee is demanded)
- bank KYC (account will be blocked unless details are updated)

**Other scam types are out of scope and are not covered**: electricity cut-off calls, TRAI or SIM-block calls, "your
relative is in trouble" calls, investment and job scams, and anything else. For such a call the stage rules have no
vocabulary and the template lines were not written for it, so Uncle mostly gives generic stalls. No claim is made
about how he behaves there.

## What exists today

- Inbound only: a test fails if the repository gains telephony or dialling code.
- Everything is synthetic: Uncle's phone number, account number and bank are invented (see `content/persona.json`).
- Replies come from reviewed template lines. The language-model fallback is off by default.
- **The safety gate is not built yet.** Until it exists, a possible "are you a person or an AI?" or an emergency gets a
  reply plan marked `needs_gate` with no text, and no voice call should be wired up.

## Measured limits (see `engineering-log.md` for the numbers and how they were obtained)

- Stage detection is a keyword layer. On 18 lines written by the project owner after the first tuning, it matched 4 of
  18 (4 of the 11 lines about the covered scam types). Most misses were "no stage claimed", where the call stays in its
  stage and Uncle stalls. That is a small set judged one line at a time, not an accuracy figure.
- Digit hearing (how well speech recognition hears numbers and letters) has not been measured yet.
