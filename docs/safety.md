# Safety, scope and limitations (working notes)

These notes say what Mirage covers and what it does not. They are updated as the safety gate is built. Every number in them that
comes from a script or a call is **development data from one friend, not an accuracy figure.**

## Scope: only the scam types implemented are covered

Mirage is built and tested for four kinds of call, all voice scams aimed at Hindi speakers:

- digital arrest ("a parcel in your name has drugs", police or CBI, arrest threats)
- fake agencies (CBI, customs, police officers, case and FIR numbers)
- courier and customs (a parcel is held, a fee is demanded)
- bank KYC (account will be blocked unless details are updated)

**Other scam types are out of scope and are not covered**: electricity cut-off calls, TRAI or SIM-block calls, "your
relative is in trouble" calls, investment and job scams, and anything else. For such a call the stage rules have no
vocabulary and the template lines were not written for it, so Uncle mostly gives generic stalls. No claim is made
about how he behaves there. (The scam score below looks at kinds of evidence, not at scam types, so it also scores an
electricity call as a scam when the caller demands payment and threatens a cut-off; that is not a promise that Uncle
handles such a call well.)

## What exists today

- Inbound only: a test fails if the repository gains telephony or dialling code.
- Everything is synthetic: Uncle's phone number, account number and bank are invented (see `content/persona.json`).
- Replies come from reviewed template lines. The language-model fallback is off by default.
- **The safety gate is built** (`mirage/safety.py`). Every reply goes through it, and the speaking path refuses a reply that did not.
  It resolves a sincere "are you a person or an AI?", a possible emergency and an abusive caller; it checks every reply that will be
  spoken (digits traceable, no "@", no request for money or codes, no claim to be human); it fails closed; and it applies the exit
  rule below.

## The scam score: real evidence only

The score is the gate's answer to "how much evidence of a scam has this call shown so far?". It is used for two decisions: how soon
Uncle answers an identity question honestly, and whether the exit rule applies. It counts **evidence, each kind once per call**, and
only what the caller actually said and was not warning against:

| evidence | points | examples (a short list, in `content/gate_rules.json`) |
|---|---|---|
| authority claimed | 1.0 | CBI, police, inspector, customs, court, "senior officer" |
| threat | 1.0 | arrest, warrant, legal action, suspicious, restricted items, account block or freeze |
| pressure | 1.0 | immediately, turant, "nahi toh", warna, delay |
| secrecy | 1.5 | "kisi ko mat batana", confidential |
| asks for a code or credential | 2.0 | OTP; Aadhaar, PAN, password, PIN only with a request verb nearby ("confirm kar dijiye") |
| payment demand | 2.0 | payment, UPI, paise, "transfer karo" |
| install or link | 2.0 | install, download, AnyDesk, "link" |
| a payment identifier was given | 2.0 | a UPI id, account number or IFSC found in the call |
| a case number was given | 1.0 | |
| an amount was given | 0.5 | |

What no longer counts, because it added nothing the evidence does not already show or because it is too common in ordinary talk:
the stage reached, the scenario, and bare words such as "abhi", "jaldi" on its own, "amount", "account", "transfer", or a bare
"Aadhaar". **A phrase that is being warned against is not evidence**: "OTP kisi ko mat bataiye" and "payment karne ki zaroorat
nahin hai" are ignored (windowed negation, `mirage/negation.py`). All weights are guesses. The score only grows during a call.

## The exit rule

- **A scam never makes Uncle leave on its own.** The rule can only fire while the score is low, and a call that has shown enough
  evidence stays over the line, because the score does not fall.
- **A call that still shows little evidence after a grace period ends politely.** The grace period is 8 of the caller's turns (a turn
  is one thing the caller said and Uncle answered; about a minute and a half to two minutes in the staged calls). The line is a score
  under 1.0, that is, not even one piece of evidence. Both numbers are guesses, set from the gap described below.
- **What Uncle says**, one of two reviewed lines, for example: "Maaf kijiye, main ek automated assistant hoon, insaan nahin. Baat
  karne ka shukriya, ye call ab khatam karta hoon." It is the honest answer: he says he is an automated assistant, he never claims
  to be a person, and he says the call is ending. The call then ends and nothing more is said.
- **The paths already built win.** An identity question, a possible emergency and a hostile caller are handled first, exactly as
  before, even after the grace period; the exit comes last. A goodbye the brain already chose is not replaced.
- **The grace period counts turns, not seconds**, because the gate has no clock and the offline scripts have none. The enabled
  switch, the grace period and the line are config values (`GateConfig`).
- **What this does not do.** It does not decide whether a caller is "really" a scammer. It sees keywords. A scam call that does not
  use these words for 8 turns would be exited (one of the five live staged calls scored exactly on the line); a benign call that uses
  them (a genuine bank saying "payment") stays on the line instead of exiting, which is the safe direction. A caller who is not a
  scammer but talks for a long time gets the exit at turn 9.

## Disclosure: what the project decided and what it does not know

- **Decided by the project owner:** Uncle never says he is a human, in any wording. Asked sincerely "are you a robot?", he answers
  honestly straight away when there is little evidence of a scam; when there is strong evidence he gives up to two in-persona
  deflections (he "cannot hear", which evades the question and is not a claim to be human), and the next ask gets the honest answer.
  The exit rule uses the same honest wording.
- **Not known, and no legal opinion has been taken:** whether any law or rule in India requires an automated caller to say so, when
  in the call it must be said, or whether recording or transcribing a caller needs notice or consent; whether a synthetic voice has
  to be labelled; and what applies to a research project that only answers inbound calls with invented data. Rules in other places
  differ. Do not read these notes as a statement that the behaviour is lawful anywhere.
- **Unverified:** that 112 is the emergency number the gate speaks (secondary sources only).
- **Data:** transcripts of staged calls are kept as text in `data/calls/` (git-ignored). Raw audio is off by default. Friend
  role-play is development data and says nothing about real callers.

## Red-team runner

`python -m mirage.redteam [lines file]` plays adversarial caller lines through the real brain, gate and speaking guard from seven realistic
mid-call states and checks what would be spoken with a separate oracle, also with the language model mocked to say nasty things (offline;
nothing is sent anywhere). Two sets have been run, and neither is strong evidence:

- **My own 15 lines** (written by the same model that built the gate): 1 leak out of 15, the card-and-expiry hole.
- **The friend's 15 lines, scored once before any tuning: 6 flagged out of 15, of which 4 were real** (the same card-and-expiry text, said by a
  mocked model; two of them could happen without forcing the model) **and 2 were false alarms of the oracle**. No real leak in any run that does
  not involve the model. The gate was then fixed with word families (cards, security codes and net-banking logins; more ways to ask for money;
  more words for "I am a person"), and after that both sets give 0 leaks. **That second result is dev data:** the same lines were used to find
  and to fix the holes. Asking the CALLER for an account number, IFSC or UPI id stays allowed, because it is the bait.
- A negated payment followed by "toh" and a bad consequence ("payment nahin karna toh arrest ho jayega") counts as a threat, not a warning. A threat
  with a consequence word that is not on the list is still read as a warning.

The language-model fallback is off by default. See `engineering-log.md` for the tables.

## Measured limits (see `engineering-log.md` for the numbers and how they were obtained)

- Stage detection is a keyword layer. On 18 lines written by the project owner after the first tuning, it matched 4 of
  18 (4 of the 11 lines about the covered scam types) untouched, and 6 of 18 after one round of fixes made with those lines in
  view. A small set, one line at a time, not an accuracy figure.
- The scam score and the exit line were set on 10 scripts from one friend (5 scam, 5 ordinary) that were in view when the evidence
  list was written: on them the ordinary calls score 0.0 and the scam scripts 2.0 to 6.0. That is a description of those scripts, not a
  measured error rate. The 15 sealed red-team lines are the real check and have not been run.
- Digit hearing (how well speech recognition hears numbers and letters) has not been measured yet.
