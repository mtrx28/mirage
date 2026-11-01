"""Send ONE tiny request to a Gemini model you name, and show what came back.

Use it to (a) check your key and a model id, and (b) see the real error text when a model's daily
quota is used up. It prints the HTTP status, the model that answered, and (for a 429) the raw error
body with any key text scrubbed.

Safety: it refuses to send more than 3 requests in total, ever. The only thing it saves is that
counter (data/smoke/gemini_smoke_count.json). Requests made here count against your Google project's
free-tier limits like any other.
"""
from __future__ import annotations

import argparse
import json
import sys

from _common import ROOT, SMOKE_DIR, confirm, scrub
from _common import _secrets  # the same key-scrubbing list the Gnani scripts use

sys.path.insert(0, str(ROOT))
from mirage.llm import GeminiClient, LLMError, load_api_key  # noqa: E402

MAX_REQUESTS_TOTAL = 3
COUNT_PATH = SMOKE_DIR / "gemini_smoke_count.json"
TIMEOUT_S = 15.0
PROMPT = "Say hello in three words."
SYSTEM = "You are a test. Reply very briefly."


def requests_so_far() -> int:
    try:
        return int(json.loads(COUNT_PATH.read_text())["sent"])
    except (OSError, ValueError, KeyError):
        return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--model", required=True, help="the exact model id string from AI Studio")
    p.add_argument("--yes", action="store_true", help="skip the confirmation question")
    p.add_argument("--dry-run", action="store_true", help="print the plan only; no key needed, nothing sent")
    args = p.parse_args()

    sent = requests_so_far()
    print(f"Plan: send 1 tiny request ({PROMPT!r}) to model {args.model!r}.")
    print(f"      requests sent by this script so far: {sent} of {MAX_REQUESTS_TOTAL}")
    if sent >= MAX_REQUESTS_TOTAL:
        sys.exit(f"Refusing: this script has already sent {sent} requests (the limit is {MAX_REQUESTS_TOTAL}).")
    if args.dry_run:
        print("Dry run: nothing was sent.")
        return

    key = load_api_key()
    if not key:
        sys.exit("GEMINI_API_KEY is missing. Put it in .env (see .env.example).")
    _secrets.append(key)
    print("GEMINI_API_KEY found: yes")
    if not confirm("Send it?", args.yes):
        sys.exit("Cancelled.")

    # Count the request before sending it, so a crash cannot hide that it happened.
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    COUNT_PATH.write_text(json.dumps({"sent": sent + 1}))

    client = GeminiClient(key, max_output_tokens=30)
    try:
        raw = client.generate_raw(args.model, SYSTEM, PROMPT, TIMEOUT_S)
    except LLMError as e:
        sys.exit(f"Request failed: {scrub(e)}")

    print(f"\nHTTP status:      {raw.status}")
    print(f"model requested:  {args.model}")
    if raw.status == 200 and raw.json:
        print(f"model answered:   {raw.json.get('modelVersion', '(not reported)')}")
        try:
            text = raw.json["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            text = "(no text in the reply)"
        print(f"reply:            {scrub(text.strip()[:200])!r}")
    else:
        label = "429 body (raw, key scrubbed)" if raw.status == 429 else "error body (key scrubbed)"
        print(f"{label}:\n{scrub(raw.text)[:3000]}")
    print(f"\nrequests sent by this script: {sent + 1} of {MAX_REQUESTS_TOTAL}")


if __name__ == "__main__":
    main()
