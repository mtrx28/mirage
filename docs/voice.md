# Voice demo: how to run it (stop 1)

One server process, one call at a time, on this computer only.

Install (once):

    python -m pip install -r requirements.txt

Start:

    python -m mirage.server --max-minutes 3 --keyterms off

Then open http://127.0.0.1:8000 in Chrome (exactly that address: the browser only gives a page the microphone on
127.0.0.1 or localhost). Press Start call. Stop the server with Ctrl+C.

- **Single process only.** A lock file stops a second server (they would share the credit ledger and the daily LLM
  counter unsafely).
- **Credits.** `python -m mirage.credits` shows the ledger; `python -m mirage.credits sync <dashboard figure> --requests <n>`
  corrects it. The hard stop is 80% of the pool.
- **Data.** Per-call evidence goes to `data/calls/<id>/`. Raw audio is NOT saved unless MIRAGE_SAVE_AUDIO=1 is set;
  saved audio goes to `data/audio/` and is deleted after 7 days at server start. `data/` is gitignored.
- **Staged demo.** The page carries a "STAGED DEMO: synthetic data" banner. All data is invented.
- **Headphones.** The friend should wear headphones for Uncle's voice and talk into the laptop microphone
  (the later half-duplex mode mutes the caller while Uncle speaks).
