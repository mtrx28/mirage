# Mirage: a scam-baiting voice agent (one-page pitch)

**The problem.** Phone scams such as "digital arrest" and fake bank-KYC calls work because the victim is alone and in a hurry. The payment details a scammer
gives out (a UPI id, an account number, a phone number) are the evidence, but they are rarely written down.

**The solution.** Mirage is an **inbound-only** voice agent. A caller who may be a scammer reaches "Rajesh Uncle", a fictional 68-year-old Hindi-speaking
retired clerk, who sounds willing, keeps the caller talking and asks them to repeat details slowly. Every payment detail is recorded with the **exact words,
the time it was said and a confidence label**. Uncle's phone number, account and bank are all invented.

**How it works.**
- **Listens** with Gnani Prisma v3.0 (streaming Hindi speech-to-text); **speaks** with Gnani Timbre v3.0.
- A **rule-based state machine** picks every action. The language model is off by default: the demo uses reviewed template lines only.
- A **safety gate** checks every line before it is spoken. Uncle never claims to be a person, never speaks a real-looking number, and leaves politely,
  saying he is an automated assistant, when a call shows no sign of a scam.
- Each call produces an **evidence packet**: the captured items with quote, time and confidence, the stage timeline, the transcript, how the call ended
  and what Uncle disclosed. A sha256 hash shows the JSON is unchanged since it was made. No claim is made about legal admissibility.

**What we measured** (staged calls, synthetic data, small samples; development results, not rates for real callers):
- **16 of 20** spoken numbers stored exactly, in 4 live calls with two speakers.
- On a friend's 15 red-team lines the first untuned run found **4 real leaks** (one hole); after the fixes, 0. That is not a claim of safety.
- Prisma's server latency was **330 ms** at the median (439 ms at p95) over 132 speech segments. **910** offline tests pass.

**Limits.** Tested only on staged, role-played calls, never on a real scammer. Replies are often generic stalls, stage detection is weak on lines it has not
seen, a digit can be stored wrong while looking confident, UPI provider names get garbled, and the caller cannot interrupt Uncle. The UPI handle list and the
emergency number 112 are unverified. We have taken no legal advice.

**See it.** The demo video and run commands are at the top of the [README](../README.md); a sample packet from the staged demo call is in
[`docs/sample/`](sample/); full numbers and their limits are in [`docs/results.md`](results.md).

*Built by one person with AI coding assistance (Claude), for the Gnani AI internship challenge.*
