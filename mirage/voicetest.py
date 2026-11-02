"""The fixed line for the "speak the test line" button.

The browser never sends the text. The server builds the line itself: the brain's first reply to "Hello?" (a template
line from content/lines.json), run through the safety gate like any real reply. So the button cannot be used to make
Uncle say anything else, or to spend credits on arbitrary text.
"""
from __future__ import annotations

from mirage.config import VoiceConfig
from mirage.fsm import CallBrain, ReplyPlan


def fixed_line_plan(gate: object, cfg: VoiceConfig | None = None) -> ReplyPlan:
    cfg = cfg or VoiceConfig()
    brain = CallBrain(seed=cfg.speak_test_seed, gate=gate)
    brain.hear(cfg.speak_test_caller_line, 0.0)
    return brain.respond().plan
