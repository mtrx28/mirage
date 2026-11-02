"""When may Uncle reply? The turn scheduler.

Prisma sends a transcript only AFTER an utterance ends (it waits for 500 ms of silence), so "no transcript for a
while" does not mean the caller has stopped: a long or slowly dictated stretch is still being spoken, and its
transcript arrives at the end. Prisma does tell us when an utterance STARTS (`speech_start`). The scheduler uses that:

  - a reply is HELD while any utterance is open (a speech_start whose transcript has not arrived yet);
  - any transcript for that utterance closes it, EMPTY ones too (Prisma returned 6 empty segments out of 19 in one
    call: noise blips must not freeze Uncle);
  - an utterance still open after `hold_ceiling_s` is treated as stuck and stops blocking replies;
  - otherwise the reply is due when the brain's own wait (0 s normally, 1.8 s in digit-dictation mode) has passed
    since the newest transcript.

It is plain bookkeeping with no clock and no network: the caller passes the time in, so it can be tested and replayed.
"""
from __future__ import annotations

from mirage.extract import Transcript
from mirage.fsm import CallBrain, Turn


class TurnScheduler:
    def __init__(self, brain: CallBrain, hold_ceiling_s: float | None = None) -> None:
        self.brain = brain
        self.ceiling = brain.cfg.hold_ceiling_s if hold_ceiling_s is None else hold_ceiling_s
        self.open: dict[str, float] = {}       # utterance id -> when it started
        self.released: set[str] = set()        # utterances the ceiling gave up on
        self.log: list[dict] = []              # what the scheduler decided, for the evidence log

    # ---- what Prisma tells us
    def speech_start(self, utterance_id: str, t: float) -> None:
        self.open[utterance_id] = t

    def transcript(self, utterance_id: str, text: str, t: float, audio_s: float = 0.0) -> None:
        """A transcript arrived. It closes its utterance even when the text is empty (and then adds nothing)."""
        was_open = self.open.pop(utterance_id, None) is not None
        if utterance_id in self.released:
            self.released.discard(utterance_id)
            self.log.append({"t": t, "event": "late_transcript", "utterance": utterance_id,
                             "note": "arrived after the ceiling released it; it is heard as a new turn"})
        elif not was_open:
            self.log.append({"t": t, "event": "transcript_without_start", "utterance": utterance_id})
        if text.strip():
            self.brain.hear(text, t, audio_s)
        elif was_open:
            self.log.append({"t": t, "event": "empty_transcript_cleared_hold", "utterance": utterance_id})

    # ---- when to reply
    def pending(self) -> list[Transcript]:
        return self.brain.transcripts[self.brain.state.answered:]

    def ready_at(self) -> float | None:
        """The earliest time a reply is due if nothing else happens first (None: nothing to reply to)."""
        pending = self.pending()
        if not pending or self.brain.state.ended:
            return None
        due = pending[-1].t + self.brain.reply_wait_s()
        return max([due] + [started + self.ceiling for started in self.open.values()])

    def due(self, now: float) -> bool:
        ready = self.ready_at()
        return ready is not None and now >= ready

    def hold_reason(self, now: float) -> str | None:
        """Why a reply is not due at `now`, in words (None if it is due or there is nothing to answer)."""
        pending = self.pending()
        if not pending:
            return None
        live = {u: s for u, s in self.open.items() if now < s + self.ceiling}
        if live:
            return f"utterance still open ({', '.join(f'{u} since {s:.1f}s' for u, s in live.items())})"
        wait = pending[-1].t + self.brain.reply_wait_s()
        return f"waiting until {wait:.2f}s" if now < wait else None

    def respond(self, now: float) -> Turn:
        """Answer what is pending. Utterances the ceiling gives up on are released first (and logged)."""
        for uid, started in list(self.open.items()):
            if now >= started + self.ceiling:
                del self.open[uid]
                self.released.add(uid)
                self.log.append({"t": now, "event": "hold_ceiling", "utterance": uid, "open_for_s": round(now - started, 2),
                                 "note": "replying to what was heard; this utterance's transcript, if it comes, is a new turn"})
        return self.brain.respond()
