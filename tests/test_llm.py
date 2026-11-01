"""Tests for mirage/llm.py. A fake client and a fake clock stand in for Gemini and real time: no network."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from mirage.config import LLMConfig, ModelBudget, default_config
from mirage.llm import (
    GeminiClient, LLMError, LLMRouter, RateLimitError, UsageStore, atomic_write_text,
    build_router, classify_429, extract_text,
)

# 1 Nov 2026 12:00 UTC. US clocks go back that morning, so Pacific midnight is 08:00 UTC from 2 Nov on.
NOON = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
DAILY_429 = '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota_exceeded: daily quota"}}'
MINUTE_429 = '{"error": {"code": 429, "message": "rate_limit_exceeded: per minute"}}'


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.now = NOON

    def wall(self) -> datetime:
        return self.now


class FakeClient:
    """Records every call. `script` maps a model id to a function that answers or raises."""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls: list[str] = []

    def generate(self, model_id, system, prompt, timeout_s):
        self.calls.append(model_id)
        behaviour = self.script.get(model_id)
        return behaviour() if behaviour else f"reply from {model_id}"


def raises(error):
    def run():
        raise error
    return run


def models(*specs) -> tuple[ModelBudget, ...]:
    """models(("A", per_minute, per_day), ...)"""
    return tuple(ModelBudget(name, name, pm, pd) for name, pm, pd in specs)


def make(tmp_path: Path, specs, client=None, clock=None, **overrides):
    clock = clock or Clock()
    cfg = dataclasses.replace(
        default_config(), models=models(*specs),
        usage_path=tmp_path / "usage.json", first_429_path=tmp_path / "first_429.txt", **overrides,
    )
    store = UsageStore(cfg.usage_path, reset_timezone=cfg.reset_timezone, reset_hour=cfg.reset_hour, wall=clock.wall)
    client = client if client is not None else FakeClient()
    router = LLMRouter(cfg, client, store, monotonic=lambda: clock.mono)
    return router, store, client, clock, cfg


# ---------------------------------------------------------------- ordering, budgets, fall-through

def test_models_are_tried_in_order_and_a_429_falls_through(tmp_path):
    client = FakeClient({"A": raises(RateLimitError(MINUTE_429))})
    router, *_ = make(tmp_path, [("A", 10, 100), ("B", 10, 100)], client)
    result = router.complete("sys", "hi")
    assert result.model_id == "B" and result.text == "reply from B"
    assert client.calls == ["A", "B"]


def test_daily_budget_stops_before_the_limit_and_falls_through(tmp_path):
    router, store, client, *_ = make(tmp_path, [("A", 100, 3), ("B", 100, 100)])
    for _ in range(4):
        router.complete("sys", "hi")
    assert client.calls == ["A", "A", "A", "B"]
    assert store.count("A") == 3


def test_everything_used_up_returns_none_and_makes_no_extra_calls(tmp_path):
    router, _, client, *_ = make(tmp_path, [("A", 100, 1), ("B", 100, 1)])
    assert router.complete("s", "p") is not None
    assert router.complete("s", "p") is not None
    assert router.complete("s", "p") is None
    assert len(client.calls) == 2


def test_at_most_two_models_are_tried_per_turn(tmp_path):
    rate = raises(RateLimitError(MINUTE_429))
    client = FakeClient({"A": rate, "B": rate, "C": rate})
    router, *_ = make(tmp_path, [("A", 10, 100), ("B", 10, 100), ("C", 10, 100)], client)
    assert router.complete("s", "p") is None
    assert client.calls == ["A", "B"]


def test_placeholder_model_ids_are_never_called(tmp_path):
    router, _, client, *_ = make(tmp_path, [("PASTE-ID-FOR-X", 10, 100), ("B", 10, 100)])
    assert router.complete("s", "p").model_id == "B"
    assert client.calls == ["B"]


def test_gemma_is_not_in_the_default_config():
    assert not any("gemma" in (m.label + m.model_id).lower() for m in default_config().models)


def test_per_minute_window_slides_with_the_clock(tmp_path):
    router, _, client, clock, _ = make(tmp_path, [("A", 2, 100)])
    assert router.complete("s", "p") and router.complete("s", "p")
    assert router.complete("s", "p") is None          # third request inside the same minute
    clock.mono += 61
    assert router.complete("s", "p") is not None
    assert client.calls == ["A", "A", "A"]


def test_with_many_threads_the_budget_is_never_exceeded(tmp_path):
    class SlowClient(FakeClient):
        def generate(self, *args):
            time.sleep(0.005)   # widen the window in which a race could happen
            return super().generate(*args)

    router, store, client, *_ = make(tmp_path, [("A", 1000, 10)], SlowClient())
    results, barrier = [], threading.Barrier(50)

    def worker():
        barrier.wait()
        results.append(router.complete("s", "p"))

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(client.calls) == 10
    assert sum(r is not None for r in results) == 10
    assert store.count("A") == 10


# ---------------------------------------------------------------- the file on disk

def test_counts_survive_a_restart(tmp_path):
    router, _, _, clock, cfg = make(tmp_path, [("A", 100, 100)])
    router.complete("s", "p")
    router.complete("s", "p")
    again = UsageStore(cfg.usage_path, reset_timezone=cfg.reset_timezone, reset_hour=cfg.reset_hour, wall=clock.wall)
    assert again.count("A") == 2


def test_a_crash_during_the_swap_leaves_the_old_file_intact(tmp_path):
    target = tmp_path / "usage.json"
    atomic_write_text(target, "old")

    def crash(*_):
        raise OSError("power cut")

    with pytest.raises(OSError):
        atomic_write_text(target, "new", replace=crash)
    assert target.read_text() == "old"
    atomic_write_text(target, "newer")          # a leftover temp file must not get in the way
    assert target.read_text() == "newer"


def test_permission_errors_are_retried(tmp_path):
    target = tmp_path / "usage.json"
    attempts = []

    def flaky(src, dst):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("antivirus has the file")
        Path(src).replace(dst)

    atomic_write_text(target, "data", replace=flaky, sleep=lambda s: None)
    assert target.read_text() == "data" and len(attempts) == 3


def test_a_failed_save_gives_no_slot_and_rolls_the_count_back(tmp_path):
    def broken(path, text):
        raise OSError("disk full")

    clock = Clock()
    store = UsageStore(tmp_path / "u.json", reset_timezone="America/Los_Angeles", reset_hour=0,
                       wall=clock.wall, writer=broken)
    assert store.try_reserve("A", 5) is False
    assert store.count("A") == 0


def test_a_corrupt_file_is_kept_and_blocks_gemini_for_the_day(tmp_path):
    (tmp_path / "usage.json").write_text("{this is not json")
    router, store, client, clock, cfg = make(tmp_path, [("A", 100, 100)])
    assert router.complete("s", "p") is None and client.calls == []
    assert (tmp_path / "usage.json.corrupt").read_text() == "{this is not json"
    assert store.is_blocked_all()
    # still blocked after a restart on the same day ...
    restarted = UsageStore(cfg.usage_path, reset_timezone=cfg.reset_timezone, reset_hour=cfg.reset_hour, wall=clock.wall)
    assert restarted.is_blocked_all()
    # ... and free again after the reset
    clock.now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    assert not restarted.is_blocked_all()


def test_the_day_rolls_over_at_the_configured_reset(tmp_path):
    router, store, _, clock, _ = make(tmp_path, [("A", 100, 1)])
    clock.now = datetime(2026, 11, 2, 7, 59, tzinfo=timezone.utc)   # 23:59 on 1 Nov in Pacific time
    assert router.complete("s", "p") is not None
    assert router.complete("s", "p") is None
    clock.now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)    # midnight Pacific
    assert router.complete("s", "p") is not None
    assert store.count("A") == 1


# ---------------------------------------------------------------- 429 handling

@pytest.mark.parametrize("body, expected", [
    (DAILY_429, "daily"),
    ("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "daily"),
    ("You exceeded your daily quota", "daily"),
    (MINUTE_429, "per_minute"),
    ("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "per_minute"),
    ("too_many_requests", "unknown"),
    ("", "unknown"),
])
def test_classify_429(body, expected):
    assert classify_429(body) == expected


def test_an_unclear_429_backs_off_then_retries_later(tmp_path):
    attempts = []

    def first_fails():
        attempts.append(1)
        if len(attempts) == 1:
            raise RateLimitError("something odd")
        return "fine"

    client = FakeClient({"A": first_fails})
    router, store, _, clock, _ = make(tmp_path, [("A", 100, 100), ("B", 100, 100)], client)
    router.complete("s", "p")                   # A fails, B answers
    router.complete("s", "p")                   # A is resting, so straight to B
    assert client.calls == ["A", "B", "B"]
    assert not store.is_spent("A")              # an unclear 429 is NOT a spent day
    clock.mono += 6                             # past the 5 s rest
    assert router.complete("s", "p").model_id == "A"


def test_a_clear_daily_429_marks_the_model_spent_even_after_a_restart(tmp_path):
    client = FakeClient({"A": raises(RateLimitError(DAILY_429))})
    router, store, _, clock, cfg = make(tmp_path, [("A", 100, 100), ("B", 100, 100)], client)
    router.complete("s", "p")
    assert store.is_spent("A")
    store2 = UsageStore(cfg.usage_path, reset_timezone=cfg.reset_timezone, reset_hour=cfg.reset_hour, wall=clock.wall)
    client2 = FakeClient()
    router2 = LLMRouter(cfg, client2, store2, monotonic=lambda: clock.mono)
    assert router2.complete("s", "p").model_id == "B"
    assert client2.calls == ["B"]
    clock.now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    assert router2.complete("s", "p").model_id == "A"


def test_the_first_429_is_logged_once_with_the_key_scrubbed(tmp_path):
    client = FakeClient({"A": raises(RateLimitError("limit hit for SECRET123 on A"))})
    router, _, _, clock, cfg = make(tmp_path, [("A", 100, 100)], client)
    router._secrets = ["SECRET123"]
    router.complete("s", "p")
    first = cfg.first_429_path.read_text()
    assert "limit hit for *** on A" in first and "SECRET123" not in first
    clock.mono += 400
    client.script["A"] = raises(RateLimitError("a second, different body"))
    router.complete("s", "p")
    assert cfg.first_429_path.read_text() == first


# ---------------------------------------------------------------- failures, deadlines, locks

def test_a_failed_request_still_uses_up_its_slot(tmp_path):
    client = FakeClient({"A": raises(LLMError("HTTP 500"))})
    router, store, *_ = make(tmp_path, [("A", 100, 100)], client)
    assert router.complete("s", "p") is None
    assert store.count("A") == 1


def test_a_hung_client_cannot_hold_us_past_the_deadline(tmp_path):
    release = threading.Event()

    class Hung(FakeClient):
        def generate(self, model_id, *args):
            if model_id == "A":
                release.wait(10)
            return super().generate(model_id, *args)

    router, *_ = make(tmp_path, [("A", 100, 100), ("B", 100, 100)], Hung(),
                      request_timeout_s=0.2, overall_deadline_s=1.0)
    try:
        started = time.monotonic()
        result = router.complete("s", "p")
        assert time.monotonic() - started < 1.0
        assert result.model_id == "B"           # A timed out, B still got its turn
    finally:
        release.set()

    only_a, *_ = make(tmp_path / "second", [("A", 100, 100)], Hung(), request_timeout_s=0.2, overall_deadline_s=0.3)
    release.clear()
    try:
        started = time.monotonic()
        assert only_a.complete("s", "p") is None
        assert time.monotonic() - started < 1.0
    finally:
        release.set()


def test_no_lock_is_held_while_the_client_call_runs(tmp_path):
    seen = {}

    class Probe(FakeClient):
        def generate(self, *args):
            seen["router_lock_free"] = router._lock.acquire(blocking=False)
            seen["store_lock_free"] = store._lock.acquire(blocking=False)
            for lock in (router._lock, store._lock):
                if lock.locked():
                    lock.release()
            return super().generate(*args)

    router, store, *_ = make(tmp_path, [("A", 100, 100)], Probe())
    assert router.complete("s", "p") is not None
    assert seen == {"router_lock_free": True, "store_lock_free": True}


def test_acomplete_runs_in_a_worker_thread_and_does_not_block_the_loop(tmp_path):
    class Slow(FakeClient):
        def generate(self, *args):
            time.sleep(0.3)
            return super().generate(*args)

    router, *_ = make(tmp_path, [("A", 100, 100)], Slow())
    threads = []
    original = router.complete

    def spy(*args):
        threads.append(threading.current_thread())
        return original(*args)

    router.complete = spy
    ticks = []

    async def scenario():
        async def ticker():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.02)
        task = asyncio.create_task(ticker())
        result = await router.acomplete("s", "p")
        task.cancel()
        return result

    assert asyncio.run(scenario()).model_id == "A"
    assert threads[0] is not threading.main_thread()
    assert len(ticks) >= 5                      # the loop kept running during the 0.3 s request


def test_without_a_client_the_answer_is_none(tmp_path):
    cfg = dataclasses.replace(default_config(), models=models(("A", 10, 10)), usage_path=tmp_path / "u.json")
    store = UsageStore(cfg.usage_path, reset_timezone=cfg.reset_timezone, reset_hour=cfg.reset_hour)
    assert LLMRouter(cfg, None, store).complete("s", "p") is None
    assert build_router(cfg, api_key="").complete("s", "p") is None


# ---------------------------------------------------------------- the real client, with a fake HTTP layer

class FakeHttp:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.sent = response, error, []

    def post(self, url, headers, json, timeout):
        self.sent.append((url, headers, json, timeout))
        if self.error:
            raise self.error
        return self.response


def test_the_key_goes_in_a_header_and_never_in_the_url_or_an_error(tmp_path):
    ok = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "hello"}]}}]})
    http = FakeHttp(ok)
    client = GeminiClient("SECRET123", http=http)
    assert client.generate("some-model", "sys", "hi", 3.0) == "hello"
    url, headers, body, _ = http.sent[0]
    assert "SECRET123" not in url and headers["x-goog-api-key"] == "SECRET123"
    assert body["contents"][0]["parts"][0]["text"] == "hi"
    assert "SECRET123" not in repr(client) and "SECRET123" not in repr(default_config())

    broken = GeminiClient("SECRET123", http=FakeHttp(error=httpx.ConnectError("boom SECRET123")))
    with pytest.raises(LLMError) as err:
        broken.generate("some-model", "sys", "hi", 3.0)
    assert "SECRET123" not in str(err.value)

    limited = GeminiClient("SECRET123", http=FakeHttp(httpx.Response(429, text="slow down SECRET123")))
    with pytest.raises(RateLimitError) as limit:
        limited.generate("some-model", "sys", "hi", 3.0)
    assert "SECRET123" not in limit.value.body


def test_a_model_id_cannot_smuggle_extra_url_parts():
    with pytest.raises(LLMError):
        GeminiClient("k", http=FakeHttp()).generate("a/../b?key=x", "s", "p", 1.0)


def test_extract_text_reads_gemini_json_and_rejects_empty_answers():
    payload = {"candidates": [{"content": {"parts": [{"text": "Namaste "}, {"text": "beta"}]}}]}
    assert extract_text(payload) == "Namaste beta"
    for bad in (None, {}, {"candidates": []}, {"candidates": [{"content": {"parts": [{"text": " "}]}}]}):
        with pytest.raises(LLMError):
            extract_text(bad)
