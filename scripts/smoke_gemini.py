"""Send ONE tiny request to a Gemini model you name, and show what came back.

Use it to (a) check your key and a model id, and (b) see the real error text when a model's daily
quota is used up. It prints the HTTP status, the model that answered, and (for a 429) the raw error
body with any key text scrubbed.

Safety: it refuses to send more than 3 requests in total. --extra-requests N (0 to 3) raises that cap
by N, so 6 is the most it can ever send. The only thing it saves is the counter
(data/smoke/gemini_smoke_count.json). Requests made here count against your Google project's
free-tier limits like any other.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from _common import ROOT, SMOKE_DIR, confirm, scrub
from _common import _secrets  # the same key-scrubbing list the Gnani scripts use

sys.path.insert(0, str(ROOT))
from mirage.llm import GeminiClient, LLMError, LLMTimeout, load_api_key  # noqa: E402

MAX_REQUESTS_TOTAL = 3
MAX_EXTRA = 3                      # --extra-requests can raise the cap by at most this much
COUNT_PATH = SMOKE_DIR / "gemini_smoke_count.json"
DEFAULT_TIMEOUT_S = 15.0
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
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                   help=f"seconds to wait for the reply (default {DEFAULT_TIMEOUT_S:g})")
    p.add_argument("--extra-requests", type=int, default=0, choices=range(MAX_EXTRA + 1), metavar=f"0-{MAX_EXTRA}",
                   help=f"raise the {MAX_REQUESTS_TOTAL}-request cap by this many (at most {MAX_EXTRA})")
    p.add_argument("--yes", action="store_true", help="skip the confirmation question")
    p.add_argument("--dry-run", action="store_true", help="print the plan only; no key needed, nothing sent")
    args = p.parse_args()

    sent = requests_so_far()
    cap = MAX_REQUESTS_TOTAL + args.extra_requests
    print(f"Plan: send 1 tiny request ({PROMPT!r}) to model {args.model!r}, waiting up to {args.timeout:g} s.")
    print(f"      requests sent by this script so far: {sent} of {cap}")
    if args.extra_requests:
        print(f"      WARNING: --extra-requests {args.extra_requests} raised the cap from {MAX_REQUESTS_TOTAL} to {cap}. "
              f"Every request counts against your Google project's quota.")
    if sent >= cap:
        sys.exit(f"Refusing: this script has already sent {sent} requests (the limit is {cap}).")
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
    started = time.monotonic()
    try:
        raw = client.generate_raw(args.model, SYSTEM, PROMPT, args.timeout)
    except LLMTimeout:
        elapsed = time.monotonic() - started
        sys.exit(f"\nTIMEOUT after {elapsed:.2f} s: no reply within {args.timeout:g} s.\n"
                 "This is NOT a quota error (a quota error would come back as an HTTP 429). The cause is unknown: "
                 "a slow model, a stuck request, or a timeout that is too short. The request may still have counted "
                 f"against your quota.\nrequests sent by this script: {sent + 1} of {cap}")
    except LLMError as e:
        elapsed = time.monotonic() - started
        sys.exit(f"\nRequest failed after {elapsed:.2f} s: {scrub(e)}\nrequests sent by this script: {sent + 1} of {cap}")
    elapsed = time.monotonic() - started

    print(f"\nHTTP status:      {raw.status}")
    print(f"request took:     {elapsed:.2f} s")
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
    print(f"\nrequests sent by this script: {sent + 1} of {cap}")


if __name__ == "__main__":
    main()
