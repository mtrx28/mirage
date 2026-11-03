"""Stop 3: the full loop. The caller speaks, Uncle answers.

    Prisma events -> TurnScheduler (when may Uncle reply?) -> brain -> safety gate -> Timbre -> page speaker

On top of the stop-1 call (microphone, Prisma, credits, limits) this adds, in plain words:

  * HOLD: no reply while the caller is still mid-utterance (see turns.py);
  * HALF-DUPLEX: while Uncle's line plays, the microphone audio sent to Prisma is replaced by silence, so his own voice
    cannot be heard as the caller. The cost: ANYTHING THE CALLER SAYS OVER UNCLE IS LOST. The caller must wait for him
    to finish. The page shows "Uncle is speaking" while the mute is on;
  * PLAYBACK DONE: the page tells us when its last audio piece has finished; the mute ends a short holdoff later (and
    after the audio's length plus a margin if the page never says);
  * LIMITS: at the time limit, the credit hard stop or the per-call cap, Uncle says one in-persona goodbye (allowed to
    go past the hard stop once, never when the whole pool is spent) and the call ends;
  * EVIDENCE: every turn (what was heard, the stage, findings, gate decision, Uncle's line, timings) is written to
    data/calls/<id>/turns.jsonl and latency.jsonl.

The safety gate is not optional: the brain is built with it and the Speaker refuses any plan it did not stamp.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from mirage.config import FSMConfig, VoiceConfig
from mirage.credits import CreditLimitReached
from mirage.fsm import CallBrain, ReplyPlan
from mirage.session import CallSession
from mirage.timbre import AudioCache, Speaker, SpeechQueue, TimbreError, UngatedPlan
from mirage.turns import TurnScheduler

log = logging.getLogger("mirage.live")

TTS_ERRORS = (TimbreError, UngatedPlan, CreditLimitReached)


def finding_dict(f) -> dict:
    return {"kind": f.kind, "value": f.value, "confidence": f.confidence, "valid": f.valid, "reason": f.reason,
            "needs_confirmation": f.needs_confirmation, "quote": f.quote, "t": round(f.t, 2), "suggested": f.suggested}


class LiveCall(CallSession):
    def __init__(self, cfg: VoiceConfig, gate: object, ledger, notify, *, key: str, send_audio, tts_client, cache: AudioCache,
                 fsm_cfg: FSMConfig | None = None, seed: int = 0, **kw) -> None:
        super().__init__(cfg, gate, ledger, notify, key=key, send_audio=send_audio, **kw)
        self.fsm_cfg = fsm_cfg or FSMConfig(synthetic_mode=cfg.synthetic_mode)
        self._gate_ms = 0.0

        def timed_gate(plan, state, context):                # the real gate, timed (the brain builds the reply, the gate checks it)
            started = time.perf_counter()
            out = self.gate(plan, state, context)
            self._gate_ms += (time.perf_counter() - started) * 1000
            return out

        self.brain = CallBrain(config=self.fsm_cfg, gate=timed_gate, seed=seed)
        self.sched = TurnScheduler(self.brain)
        self.queue = SpeechQueue(tts_client, cfg, ledger, cache=cache, on_charge=self._charge)
        self.speaker = Speaker(self.gate, self.queue)
        self._browser_rate = 0
        # the turn-taking state
        self._speaking = False                   # Uncle's audio is playing: the microphone is muted for Prisma
        self._replying = False                   # a reply is being made or spoken
        self._ending_reason: str | None = None   # a limit was hit: the goodbye is on its way
        self._timer: asyncio.Task | None = None
        self._reply_task: asyncio.Task | None = None
        self._deferred: list[tuple] = []         # Prisma events that arrived while Uncle was speaking
        self._mute_started = 0.0
        self._open_at_mute: set[str] = set()
        self._fed_while_speaking = 0
        self._playback_done = asyncio.Event()
        self._playing_t: float | None = None
        self._page_report: dict = {}
        self._last_meta = {"arrival": 0.0, "latency_ms": None}
        # what we report afterwards
        self.turn_records: list[dict] = []
        self.tts_failures_in_a_row = 0
        self.unspoken_turns = 0
        self.mute_anomalies = 0
        self.capture_stalls = 0
        self.playback_missing = 0
        self.stage_timeline: list[dict] = []

    # ------------------------------------------------------------------ start, audio in, page messages
    async def start(self, browser_rate: int) -> None:
        self._browser_rate = browser_rate
        await super().start(browser_rate)

    def feed(self, pcm: bytes) -> None:
        if self._speaking:
            self._fed_while_speaking += len(pcm)          # to check that the microphone kept arriving while Uncle spoke
        super().feed(pcm)

    async def on_page_message(self, data: dict) -> None:
        kind = data.get("type")
        if kind == "playing":
            self._playing_t = self.log.now()
            self._page_report["output_latency_ms"] = data.get("output_latency_ms")
        elif kind == "playback_done":
            self._page_report.update(underruns=data.get("underruns"), audio_state=data.get("audio_state"))
            self._playback_done.set()
        elif kind == "page_warning":
            message = str(data.get("message", ""))[:300]
            self.log.event("page_warning", code=str(data.get("code", ""))[:40], message=message)
            await self.notify({"type": "notice", "message": message})

    def _charge(self, chars: int) -> None:
        self.meter.tts_chars += chars

    # ------------------------------------------------------------------ Prisma events -> scheduler
    async def _on_prisma(self, msg: dict) -> None:
        await super()._on_prisma(msg)
        kind, uid = msg.get("type"), msg.get("utterance_id")
        if self._ending or not uid:
            return
        if kind == "speech_start":
            self._route(("start", uid, 0.0, "", None))
        elif kind == "transcript":
            self._route(("text", uid, (msg.get("audio_duration_ms") or 0) / 1000.0, (msg.get("text") or ""), msg.get("latency")))

    def _route(self, event: tuple) -> None:
        if self._speaking:
            self._deferred.append(event)                  # handled after Uncle stops, counted from then
            self._check_mute_leak(event)
            return
        self._apply(event, self.log.now())

    def _apply(self, event: tuple, t: float) -> None:
        kind, uid, audio_s, text, latency_ms = event
        if kind == "start":
            self.sched.speech_start(uid, t)
        else:
            self.sched.transcript(uid, text, t, audio_s)
            if text.strip():
                self._last_meta = {"arrival": t, "latency_ms": latency_ms}
        self._reschedule()

    def _check_mute_leak(self, event: tuple) -> None:
        """While muted we send only silence. Prisma reporting NEW speech well after the mute began means the caller talked
        over Uncle, or his voice echoed into the microphone, or Prisma mishandles silence. We cannot tell which, so we say so."""
        kind, uid, _, text, _ = event
        since = self.log.now() - self._mute_started
        if since <= self.cfg.mute_grace_s:
            return
        if kind == "start" or (text.strip() and uid not in self._open_at_mute):
            self.mute_anomalies += 1
            self.log.event("mute_anomaly", prisma_event=kind, utterance=uid, seconds_into_mute=round(since, 2), text=text[:80])
            if self.mute_anomalies == 1:
                self._spawn(self.notify({"type": "notice", "message": (
                    "Prisma reported speech while Uncle's line was playing, although only silence was sent to it. Either "
                    "someone talked over Uncle, or his voice reached the microphone (use headphones), or Prisma does not "
                    "treat the silent audio as silence. See the call's log.")}))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.append(task)

    # ------------------------------------------------------------------ when to reply
    def _reschedule(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = None
        if self._speaking or self._replying or self._ending or self._ending_reason:
            return
        ready = self.sched.ready_at()
        if ready is None:
            return
        self._timer = asyncio.create_task(self._wait_then_reply(max(0.0, ready - self.log.now())))
        self._tasks.append(self._timer)

    async def _wait_then_reply(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._timer = None
        if self._speaking or self._replying or self._ending or self._ending_reason:
            return
        if not self.sched.due(self.log.now()):                   # timers can wake a few ms early: look again, never reply early
            self._reschedule()
            return
        self._reply_task = asyncio.create_task(self._reply())
        self._tasks.append(self._reply_task)

    async def _reply(self) -> None:
        self._replying = True
        rec: dict[str, Any] = {"interrupted": True}
        recorded = False

        def record_turn() -> None:
            # once only, and BEFORE the call is finished: _finish writes summary.json, which counts the turn records
            # (friend session: the goodbye turn that ended call 5 was missing from the summary's counts)
            nonlocal recorded
            if not recorded:
                recorded = True
                self.turn_records.append(rec)
                self.log.record("turns", **rec)

        try:
            now = self.log.now()
            pending = self.sched.pending()
            base_wait = self.brain.reply_wait_s()
            digit_mode = self.brain.in_digit_mode()
            events_before = len(self.sched.log)
            self._gate_ms = 0.0
            started = time.perf_counter()
            turn = self.sched.respond(now)
            total_ms = (time.perf_counter() - started) * 1000
            plan = turn.plan
            last_arrival = pending[-1].t
            caller_end = last_arrival - (self._last_meta["latency_ms"] or 0) / 1000.0 - self.cfg.min_silence_ms / 1000.0
            moved = turn.stage.previous != turn.stage.stage
            if moved:
                self.stage_timeline.append({"t": round(now, 2), "turn": self.brain.state.turn, "from": turn.stage.previous, "to": turn.stage.stage})
            rec = {
                "turn": self.brain.state.turn, "reply_started_s": round(now, 3), "heard": [t.text for t in pending],
                "stage_from": turn.stage.previous, "stage": turn.stage.stage, "stage_reasons": list(turn.stage.reasons),
                "scenario": turn.scenario, "signals": list(turn.signals),
                "findings": [finding_dict(f) for f in turn.findings],
                "tactic": plan.tactic, "source": plan.source, "line_id": plan.line_id, "gate_action": plan.gate_action,
                "gate_notes": list(plan.gate_notes), "end_call": plan.end_call, "uncle_text": plan.text, "gated": plan.gated,
                "scheduler_events": self.sched.log[events_before:],
                "wait": {"wait_s": round(now - last_arrival, 3), "base_wait_s": base_wait, "digit_mode": digit_mode,
                         "hold_extra_s": round(max(0.0, now - last_arrival - base_wait), 3)},
                "timing": {"brain_ms": round(max(0.0, total_ms - self._gate_ms), 2), "gate_ms": round(self._gate_ms, 2),
                           "caller_end_est_s": round(caller_end, 3), "last_transcript_s": round(last_arrival, 3)},
                "spoken": False, "brain_believes_spoken": bool(plan.text), "interrupted": False,
            }
            await self.notify({"type": "turn", "turn": rec["turn"], "heard": " ".join(rec["heard"]), "stage_from": rec["stage_from"],
                               "stage": rec["stage"], "stage_reasons": rec["stage_reasons"], "scenario": rec["scenario"],
                               "signals": rec["signals"], "tactic": rec["tactic"], "gate_action": rec["gate_action"],
                               "uncle_text": rec["uncle_text"], "wait": rec["wait"], "timing": rec["timing"],
                               "intel": [finding_dict(f) for fs in self.brain.state.intel.values() for f in fs],
                               "stage_timeline": self.stage_timeline})
            if plan.text:
                await self._say(plan, rec)
            if (plan.end_call or self.brain.state.ended) and not self._ending and not self._ending_reason:
                record_turn()
                await self._finish("uncle_ended" if plan.text else "gate_ended")
        except asyncio.CancelledError:
            raise
        except Exception as e:                               # never fail silently: say so on the page and in the log
            log.exception("a reply failed")
            self.log.event("internal_error", error=repr(e)[:300])
            await self.notify({"type": "notice", "message": f"Something went wrong while Uncle was replying ({type(e).__name__}); the call was ended. See the call's log."})
            record_turn()
            await self._finish("internal_error")
        finally:
            record_turn()
            self._replying = False
            self._reschedule()

    # ------------------------------------------------------------------ speaking and the mute
    async def _say(self, plan: ReplyPlan, rec: dict, *, past_limit: bool = False, final: bool = False) -> None:
        cfg = self.cfg
        sent = {"bytes": 0, "first": None}
        self._playback_done = asyncio.Event()
        self._playing_t = None
        self._page_report = {}
        requested = self.log.now()

        async def forward(piece: bytes) -> None:
            if sent["first"] is None:
                sent["first"] = self.log.now()
                await self._begin_speaking(plan, rec)
            sent["bytes"] += len(piece)
            if self.send_audio:
                try:
                    await self.send_audio(piece)
                except Exception:                          # the page went away; hangup will follow
                    pass

        result, error = None, None
        try:
            result = await self.speaker.say(plan, forward, past_limit=past_limit)
            if result is None:
                error = "the line was replaced by a newer one before it was spoken"
        except TTS_ERRORS as e:
            error = str(e)
            if isinstance(e, UngatedPlan):
                self.log.event("ungated_plan_refused", message=error)
        try:
            if sent["bytes"]:
                await self._finish_playback(sent["bytes"], rec)
        finally:
            if self._speaking:
                self._unmute()
        if error is None:
            self.tts_failures_in_a_row = 0
            lat = self._latency(rec, result, sent["first"], requested)
            rec.update(spoken=True, brain_believes_spoken=True, tts_error=None, latency=lat)
            self.log.record("latency", **lat)
            await self.notify({"type": "latency", **lat})
        else:
            # The brain thinks it said this line (its memory of lines used, its tactic counters). Mark it, so a count of
            # Uncle's replies (E5) does not include a line that was never heard.
            self.tts_failures_in_a_row += 1
            self.unspoken_turns += 1
            rec.update(spoken=bool(sent["bytes"]) and "partial", brain_believes_spoken=True, tts_error=error)
            self.log.event("tts_failed", turn=rec.get("turn"), error=error, partial_bytes=sent["bytes"])
            await self.notify({"type": "notice", "message": f"Uncle could not speak this turn: {error}"})
            if not final and self.tts_failures_in_a_row >= cfg.max_tts_failures:
                await self._finish("tts_failed")

    def _latency(self, rec: dict, result, first_sent: float | None, requested: float) -> dict:
        timing, wait = rec["timing"], rec["wait"]
        playing = self._playing_t
        return {
            "turn": rec["turn"], "cached": result.cached, "chars": result.chars, "attempts": result.attempts,
            "wait_s": wait["wait_s"], "base_wait_s": wait["base_wait_s"], "hold_extra_s": wait["hold_extra_s"],
            "digit_mode": wait["digit_mode"], "brain_ms": timing["brain_ms"], "gate_ms": timing["gate_ms"],
            "tts_first_byte_ms": None if result.first_byte_s is None else round(result.first_byte_s * 1000),
            "tts_total_ms": round(result.total_s * 1000),
            "first_audio_sent_s": None if first_sent is None else round(first_sent, 3),
            "page_playing_s": None if playing is None else round(playing, 3),
            "arrival_to_audible_s": None if playing is None else round(playing - timing["last_transcript_s"], 3),
            "end_to_audible_est_s": None if playing is None else round(playing - timing["caller_end_est_s"], 3),
            "audio_s": round(result.seconds, 2), "underruns": self._page_report.get("underruns"),
            "output_latency_ms": self._page_report.get("output_latency_ms"),
        }

    async def _begin_speaking(self, plan: ReplyPlan, rec: dict) -> None:
        self._speaking = True
        self._mute_started = self.log.now()
        self._open_at_mute = set(self.sched.open)
        self._fed_while_speaking = 0
        self.pacer.muted = True                              # from here on Prisma is sent silence, not the microphone
        self.log.event("uncle_speaking_start", turn=rec.get("turn"), text=plan.text)
        await self.notify({"type": "uncle_speech_start", "turn": rec.get("turn"), "text": plan.text,
                           "sample_rate": self.cfg.tts_sample_rate})
        await self.notify({"type": "mic", "muted": True})

    async def _finish_playback(self, sent_bytes: int, rec: dict) -> None:
        cfg = self.cfg
        audio_s = sent_bytes / 2 / cfg.tts_sample_rate
        await self.notify({"type": "uncle_speech_end", "turn": rec.get("turn"), "audio_s": round(audio_s, 2)})
        try:
            await asyncio.wait_for(self._playback_done.wait(), audio_s + 0.5 + cfg.playback_margin_s)
        except asyncio.TimeoutError:
            self.playback_missing += 1
            self.log.event("playback_done_missing", turn=rec.get("turn"), waited_s=round(audio_s + 0.5 + cfg.playback_margin_s, 2))
            await self.notify({"type": "notice", "message": (
                "The page did not report that Uncle's line finished playing, so the microphone was reopened by timer. "
                "Playback may be blocked or stalled in this browser.")})
        await asyncio.sleep(cfg.unmute_holdoff_s)

    def _unmute(self) -> None:
        cfg = self.cfg
        duration = self.log.now() - self._mute_started
        expected = self._browser_rate * 2 * duration
        if duration >= cfg.capture_stall_min_s and expected and self._fed_while_speaking < 0.2 * expected:
            self.capture_stalls += 1
            self.log.event("capture_stalled_during_playback", seconds=round(duration, 2), received_bytes=self._fed_while_speaking,
                           expected_bytes=int(expected))
            if not self._ending:
                self._spawn(self.notify({"type": "notice", "message": (
                    "While Uncle was speaking, the page stopped sending microphone audio. Capturing and playing at the same "
                    "time may not work in this browser. Use the 'Check audio' button and see the call's log.")}))
        self.pacer.muted = False
        self._speaking = False
        self.log.event("uncle_speaking_end", seconds=round(duration, 2), muted_frames=self.pacer.muted_frames)
        if not self._ending:                                  # after hang-up the page already has its "ended"
            self._spawn(self.notify({"type": "mic", "muted": False}))
        deferred, self._deferred = self._deferred, []
        now = self.log.now()
        for event in deferred:                               # what was said while Uncle spoke counts from now
            self._apply(event, now)

    # ------------------------------------------------------------------ limits: one goodbye, then the end
    async def _ticker(self) -> None:
        cfg = self.cfg
        while not self._ending:
            await asyncio.sleep(cfg.tick_s)
            self._sync_meter()
            self.meter.flush()
            held = self.log.now()
            extra = self.meter.credits - self.meter._flushed
            await self.notify({
                "type": "tick", "held_s": round(held, 1), "call_credits": round(self.meter.credits, 3),
                "ledger_spent": round(self.ledger.spent + extra, 2), "pool": cfg.pool_credits,
                "pool_fraction": round(self.ledger.fraction_of_pool(extra), 4), "warning": self.ledger.warning(extra),
            })
            if self._ending_reason is None:
                reason = ("time_limit" if held >= self.max_call_s else "credit_limit" if self.meter.over_pool_limit()
                          else "call_credit_cap" if self.meter.over_call_cap() else None)
                if reason:
                    self._ending_reason = reason
                    self._spawn(self._graceful_end(reason))

    async def _graceful_end(self, reason: str) -> None:
        cfg = self.cfg
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        if self._reply_task is not None and not self._reply_task.done():       # let a line in progress finish
            await asyncio.wait({self._reply_task}, timeout=cfg.goodbye_deadline_s / 2)
        self._sync_meter()
        self.meter.flush(force=True)                                           # the ledger must be current before the goodbye
        rec: dict[str, Any] = {"forced": reason, "turn": self.brain.state.turn + 1, "heard": [], "tactic": "goodbye_wrap",
                               "timing": {"brain_ms": 0, "gate_ms": 0, "last_transcript_s": self.log.now(), "caller_end_est_s": self.log.now()},
                               "wait": {"wait_s": 0, "base_wait_s": 0, "hold_extra_s": 0, "digit_mode": False}}
        try:
            if not self.brain.state.ended and not self._ending:
                plan = self.brain.force_goodbye()
                rec.update(uncle_text=plan.text, gate_action=plan.gate_action, gated=plan.gated, line_id=plan.line_id,
                           brain_believes_spoken=bool(plan.text), spoken=False)
                if plan.text:
                    try:
                        await asyncio.wait_for(self._say(plan, rec, past_limit=True, final=True), cfg.goodbye_deadline_s)
                    except asyncio.TimeoutError:
                        rec["tts_error"] = "the goodbye did not finish inside its deadline"
                        self.log.event("goodbye_deadline", seconds=cfg.goodbye_deadline_s)
        finally:
            self.log.record("turns", **rec)
            self.turn_records.append(rec)
            await self._finish(reason)

    # ------------------------------------------------------------------ summary
    def _extra_summary(self) -> dict:
        findings = {kind: [finding_dict(f) for f in fs] for kind, fs in self.brain.state.intel.items()}
        self.log.write_json("findings.json", findings)
        return {
            "turns": len(self.turn_records), "unspoken_turns": self.unspoken_turns,
            "uncle_lines_spoken": sum(1 for r in self.turn_records if r.get("spoken") is True),
            "stage_timeline": self.stage_timeline, "final_stage": self.brain.state.stage, "scenario": self.brain.state.scenario,
            "gate_log": self.brain.state.gate_log, "scheduler_log": self.sched.log,
            "mute_anomalies": self.mute_anomalies, "capture_stalls": self.capture_stalls,
            "playback_done_missing": self.playback_missing, "findings": {k: [f["value"] for f in v] for k, v in findings.items()},
        }
