"""Replay a saved call through the brain, offline: transcript text and times only. No network, no audio.

    python -m mirage.replay data/calls/<call id>
    python -m mirage.replay tests/fixtures/calls/good_mic_keyterms_off.json --drop-last 1 --normal-wait 0.3

It feeds Prisma's own events (`speech_start` and transcripts, with their arrival times) to the turn scheduler and the
real brain and gate, firing a reply whenever the scheduler says one is due, as the live loop will.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from mirage.config import FSMConfig
from mirage.fsm import CallBrain
from mirage.safety import Gate
from mirage.turns import TurnScheduler


@dataclass
class ReplayResult:
    turns: list[dict]
    brain: CallBrain
    scheduler: TurnScheduler


def load_call(path: Path) -> dict:
    """A call as {"speech_starts": [{utterance_id, t}], "transcripts": [{utterance_id, t, text, audio_s}]}.
    Accepts a fixture .json or a data/calls/<id> folder."""
    path = Path(path)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    transcripts = [json.loads(l) for l in (path / "transcripts.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    events = [json.loads(l) for l in (path / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    return {"speech_starts": [{"utterance_id": e["utterance_id"], "t": e["t"]} for e in events if e["event"] == "speech_start"],
            "transcripts": [{"utterance_id": r["utterance_id"], "t": r["t"], "text": r["text"], "audio_s": r["audio_s"]}
                            for r in transcripts]}


def replay(call: dict, *, normal_wait_s: float | None = None, ceiling_s: float | None = None, total_cap_s: float | None = None,
           drop_last: int = 0, synthetic: bool = True, seed: int = 0) -> ReplayResult:
    transcripts = list(call["transcripts"])
    starts = list(call["speech_starts"])
    if drop_last:                                   # pretend the caller did not say the last N things
        with_text = [r for r in transcripts if r["text"].strip()]
        gone = {r["utterance_id"] for r in with_text[-drop_last:]}
        transcripts = [r for r in transcripts if r["utterance_id"] not in gone]
        starts = [s for s in starts if s["utterance_id"] not in gone]

    cfg = FSMConfig(synthetic_mode=synthetic) if normal_wait_s is None else FSMConfig(synthetic_mode=synthetic, normal_wait_s=normal_wait_s)
    brain = CallBrain(config=cfg, gate=Gate(digit_style=cfg.digit_style, seed=seed), seed=seed)
    sched = TurnScheduler(brain, ceiling_s, total_cap_s)

    # Prisma's events in the order they arrived. At the same instant a transcript counts as arriving BEFORE a
    # speech_start: the pessimistic order (the scheduler cannot know about a start that has not been delivered yet).
    events = sorted([(r["t"], 0, "transcript", r) for r in transcripts] + [(s["t"], 1, "speech_start", s) for s in starts],
                    key=lambda e: (e[0], e[1]))
    turns: list[dict] = []

    def fire_due(before: float | None) -> None:
        while True:
            ready = sched.ready_at()
            if ready is None or (before is not None and ready > before):
                return
            heard = [t.text for t in sched.pending()]
            turn = sched.respond(ready)
            turns.append({"at": round(ready, 2), "heard": heard, "stage": f"{turn.stage.previous}->{turn.stage.stage}",
                          "tactic": turn.plan.tactic, "text": turn.plan.text, "gate": turn.plan.gate_action,
                          "findings": [(f.kind, f.value, f.confidence, f.quote) for f in turn.findings]})

    for t, _, kind, data in events:
        fire_due(before=t)
        if kind == "speech_start":
            sched.speech_start(data["utterance_id"], t)
        else:
            sched.transcript(data["utterance_id"], data["text"], t, data["audio_s"])
    fire_due(before=None)
    return ReplayResult(turns, brain, sched)


def show(result: ReplayResult, title: str) -> None:
    print(f"\n######## {title}")
    for i, turn in enumerate(result.turns, 1):
        print(f"  turn {i}: Uncle replies at t={turn['at']:.2f}s ({turn['tactic']}, gate {turn['gate']}), "
              f"answering {len(turn['heard'])} transcript(s):")
        for text in turn["heard"]:
            print(f"      heard {text[:90]!r}")
        for kind, value, confidence, quote in turn["findings"]:
            print(f"      finding {kind:<9} {value!r} [{confidence}] from {quote[:60]!r}")
        print(f"      Uncle: {turn['text']!r}")
    print("  stored intel at the end:")
    for kind, items in result.brain.state.intel.items():
        for f in items:
            print(f"      {kind:<9} {f.value!r} [{f.confidence}] valid={f.valid}")
    for entry in result.scheduler.log:
        print(f"  scheduler: {entry}")


def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Replay a saved call through the brain (offline).")
    p.add_argument("call", type=Path, help="a data/calls/<id> folder or a fixture .json")
    p.add_argument("--normal-wait", type=float, default=None, help="the brain's normal wait after a transcript, seconds (default: the config value)")
    p.add_argument("--ceiling", type=float, default=None, help="hold ceiling in seconds (default: the config value)")
    p.add_argument("--total-cap", type=float, default=None, help="total hold cap in seconds (default: the config value)")
    p.add_argument("--drop-last", type=int, default=0, help="leave out the last N things the caller said")
    args = p.parse_args(argv)
    result = replay(load_call(args.call), normal_wait_s=args.normal_wait, ceiling_s=args.ceiling, total_cap_s=args.total_cap,
                    drop_last=args.drop_last)
    show(result, f"{args.call.name} (normal wait {result.brain.cfg.normal_wait_s:g}s, ceiling {result.scheduler.ceiling:g}s, "
                 f"dropped last {args.drop_last})")


if __name__ == "__main__":
    main()
