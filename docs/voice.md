# Voice demo: how to run it (stops 1 and 2)

One server process, one call at a time, on this computer only.

Install (once):

    python -m pip install -r requirements.txt

Start (keyterms are off by default; there is no evidence yet that they help):

    python -m mirage.server --max-minutes 3

Then open http://127.0.0.1:8000 in Chrome (exactly that address: the browser only gives a page the microphone on
127.0.0.1 or localhost). Stop the server with Ctrl+C.

- **Stop 1, listening:** press Start call and speak. Uncle does not answer yet; the page shows what Prisma heard.
- **Stop 2, a voice test:** press "Speak the test line". The server plays one fixed line in Uncle's voice (Prakash). The
  page cannot choose the text. The first press asks Timbre (costs a little); pressing again plays it from memory (free).
  The test is refused while a call is running.
- **A good microphone matters most.** A headphone microphone gave thin audio and lost words; a proper microphone fixed
  it. Use a good microphone for the caller, and headphones only for Uncle's voice, so his voice does not feed back.
- **Single process only.** A lock file stops a second server (they would share the credit ledger and the daily LLM
  counter unsafely).
- **Credits.** `python -m mirage.credits` shows the ledger; `python -m mirage.credits sync <dashboard figure> --requests <n>`
  corrects it. The ledger is an estimate and leans high. The hard stop is 80% of the pool.
- **Data.** Per-call evidence goes to `data/calls/<id>/`; every spoken line is logged to `data/tts_log.jsonl`
  (timing, characters, credits). Raw audio is NOT saved unless MIRAGE_SAVE_AUDIO=1 is set; saved audio goes to
  `data/audio/` and is deleted after 7 days at server start. `data/` is gitignored.
- **Staged demo.** The page carries a "STAGED DEMO: synthetic data" banner. All data is invented.
- **Half-duplex (stop 3).** While Uncle speaks, the caller's audio will be replaced by silence, so talking over him is
  lost. This is a known limitation of the first version.
