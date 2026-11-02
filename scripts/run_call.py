"""Talk to Rajesh Uncle in the terminal. You type what the scam caller says; it shows what the brain decided.

No audio and no network. The safety gate is ON (--no-gate shows what the brain says without it). The LLM fallback
is off; --llm turns it on and DOES call Gemini.

Typing:
  some text            the caller says it; Uncle replies
  + some text          the caller said it but is not done (a number dictated in parts): Uncle waits and
                       joins it with the next line
  :expected KIND       tell the brain what Uncle just asked for (phone, account, case, or none)
  :state               stage, scenario, triggers seen, obstacles used, recent lines
  :findings            everything found so far, with the quote it came from
  :quit                end
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from mirage.config import FSMConfig  # noqa: E402
from mirage.fsm import CallBrain  # noqa: E402
from mirage.safety import Gate  # noqa: E402

SECONDS_PER_WORD = 0.45     # a made-up speaking speed, so joined lines get believable timings


def show_findings(brain: CallBrain) -> None:
    items = [f for fs in brain.state.intel.values() for f in fs]
    if not items:
        print("  (nothing found yet)")
    for f in items:
        flags = [] if f.valid else ["invalid: " + f.reason]
        if f.needs_confirmation:
            flags.append("needs confirmation")
        extra = f" suggested={f.suggested!r}" if f.suggested else ""
        print(f"  {f.kind:<9} {f.value!r}  [{f.confidence}] {'; '.join(flags)}{extra}")
        print(f"            quote: {f.quote!r}  (transcript {f.source_index}, t={f.t:.1f}s)")


def show_state(brain: CallBrain) -> None:
    st = brain.state
    print(f"  stage {st.stage}, scenario {st.scenario}, turn {st.turn}, awaiting digits: {st.awaiting_digits}, "
          f"expected: {st.expected}")
    print(f"  triggers seen: {sorted(st.flags) or '-'}; obstacles used: {sorted(st.used_obstacles) or '-'}")
    print(f"  recent lines: {list(st.recent) or '-'}; readbacks: {st.readbacks}; call ended: {st.ended}")
    print(f"  intel kinds: {sorted(st.intel) or '-'}")


def show_turn(turn) -> None:
    d, plan = turn.stage, turn.plan
    move = f"{d.previous} -> {d.stage}" if d.previous != d.stage else f"{d.stage} (stayed)"
    print(f"  stage:    {move}")
    for reason in d.reasons:
        print(f"            - {reason}")
    print(f"  scenario: {turn.scenario}   signals: {', '.join(turn.signals) or '-'}")
    if turn.findings:
        print("  findings this turn:")
        for f in turn.findings:
            print(f"            {f.kind} {f.value!r} [{f.confidence}] quote={f.quote!r}")
    if plan.needs_gate:
        print("  *** NEEDS GATE: " + str(plan.gate_reason) + " ***")
        print("  No line was produced. The safety gate is off (--no-gate), so Uncle says nothing here.")
    else:
        print(f"  tactic:   {plan.tactic}  ({plan.why})")
        print(f"  source:   {plan.source}" + (f"  line {plan.line_id}" if plan.line_id else ""))
        if plan.gate_action:
            print(f"  GATE:     {plan.gate_action}: {'; '.join(plan.gate_notes)}")
        print(f"  Uncle:    {plan.text if plan.text else '(nothing: the gate failed closed or the call is over)'}")
        if plan.end_call:
            print("  *** THE CALL ENDS HERE ***")
        for s in plan.digit_sources:
            origin = (f"persona.{s.field}" if s.origin == "persona" else f"the gate's config ({s.field})"
                      if s.origin == "gate" else f"caller's words {s.quote!r}")
            print(f"            digits {s.digits!r} came from {origin}")
    mode = " (digit mode)" if turn.wait_s > FSMConfig().normal_wait_s else ""
    print(f"  wait before the next reply: {turn.wait_s:g} s{mode}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n".join(__doc__.split("\n")[2:]))
    p.add_argument("--scenario", choices=["digital_arrest", "bank_kyc"], help="force the scenario instead of detecting it")
    p.add_argument("--llm", action="store_true", help="turn the Gemini fallback ON (it calls the network; off by default)")
    p.add_argument("--no-gate", action="store_true", help="run WITHOUT the safety gate (for looking at the raw brain only)")
    p.add_argument("--synthetic", action="store_true", help="accept the staged 00-prefixed phone numbers")
    p.add_argument("--seed", type=int, default=0, help="seed for choosing between line variants")
    p.add_argument("--digit-wait", type=float, default=FSMConfig().digit_wait_s, help="seconds to wait in digit mode")
    args = p.parse_args()

    router = None
    if args.llm:
        from mirage.llm import build_router
        router = build_router()
        print("WARNING: --llm is on. Stall questions may be sent to Gemini (free tier, counted against your budget).")
    cfg = FSMConfig(digit_wait_s=args.digit_wait, synthetic_mode=args.synthetic)
    gate = None if args.no_gate else Gate(digit_style=cfg.digit_style, seed=args.seed)
    brain = CallBrain(config=cfg, scenario=args.scenario, seed=args.seed, router=router, llm_enabled=args.llm, gate=gate)

    print("Rajesh Uncle, terminal mode. Type the caller's line. :quit to stop. No audio, no network"
          + (", LLM ON" if args.llm else ", LLM off") + (", GATE OFF." if args.no_gate else ", gate on."))
    t = 0.0
    previous_was_continuation = False
    while True:
        try:
            raw = input("\ncaller> ").strip()
        except EOFError:
            break
        if not raw:
            continue
        if raw == ":quit":
            break
        if raw == ":state":
            show_state(brain)
            continue
        if raw == ":findings":
            show_findings(brain)
            continue
        if raw.startswith(":expected"):
            value = raw.split(maxsplit=1)[1].strip().lower() if " " in raw else "none"
            brain.state.expected = None if value in ("none", "") else value
            print(f"  expected hint set to {brain.state.expected}")
            continue
        if raw.startswith(":"):
            print("  unknown command")
            continue

        continuing = raw.startswith("+")
        text = raw[1:].strip() if continuing else raw
        words = len(text.split())
        # A line that follows a "+" line arrives right behind it (the caller is still speaking); any other line is a new turn.
        t += 0.9 if (continuing or previous_was_continuation) else 6.0
        previous_was_continuation = continuing
        brain.hear(text, t, audio_s=max(1.0, SECONDS_PER_WORD * words))
        if continuing:
            print(f"  (heard; not replying yet. Next line is joined to it. Wait would be {brain.reply_wait_s():g} s)")
            continue
        show_turn(brain.respond())
        if brain.state.ended:
            print("  (Uncle has said goodbye: the call is over. :quit to leave, or keep typing to see what happens.)")


if __name__ == "__main__":
    main()
