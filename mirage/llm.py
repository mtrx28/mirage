"""The only door to an LLM. It answers with text, or with None, and None means "use a template".

Free Gemini limits are tiny, so this file is mostly bookkeeping:
  - an ordered list of models, each with a per-minute and a per-day budget (from config);
  - a daily counter kept on disk, so a restart or crash does not hand us a fresh quota;
  - a slot is reserved (and saved) BEFORE a request goes out, so we can never exceed a budget,
    even with many calls at once;
  - short deadlines: a late answer is worse than a template.

Run the server as ONE process. The counter file is safe against threads, not against two processes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol
from zoneinfo import ZoneInfo

import httpx

from mirage.config import ROOT, LLMConfig, ModelBudget, default_config

log = logging.getLogger("mirage.llm")


# --------------------------------------------------------------------------- errors and the client shape

class LLMError(Exception):
    """A request failed. The message never contains the API key."""


class LLMTimeout(LLMError):
    """No reply within the timeout. Says nothing about quota: the model may be slow or the request stuck."""


class RateLimitError(LLMError):
    """HTTP 429. `body` is the server's raw text, kept so classify_429 can read it."""

    def __init__(self, body: str):
        super().__init__("HTTP 429")
        self.body = body


class LLMClient(Protocol):
    def generate(self, model_id: str, system: str, prompt: str, timeout_s: float) -> str: ...


def classify_429(body: str) -> str:
    """Decide what a 429 means: "daily", "per_minute" or "unknown".

    The one place to edit once we see real error text. The current wording rules are guesses from Google's
    docs page on errors ("quota_exceeded" = daily, "rate_limit_exceeded" = per minute or second) plus the
    "PerDay"/"PerMinute" names that quota messages usually carry. Daily is checked first because a
    daily message can also mention a per-minute limit.
    """
    text = body.lower().replace("_", " ").replace("-", " ")
    if any(word in text for word in ("quota exceeded", "perday", "per day", "daily")):
        return "daily"
    if any(word in text for word in ("rate limit exceeded", "perminute", "per minute", "per second", "persecond")):
        return "per_minute"
    return "unknown"


def scrub(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


# --------------------------------------------------------------------------- the real Gemini client

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_SAFE_MODEL_ID = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class RawResponse:
    status: int
    text: str
    json: dict | None


def extract_text(payload: dict | None) -> str:
    """Pull the reply out of Gemini's JSON: candidates[0].content.parts[*].text."""
    try:
        parts = payload["candidates"][0]["content"]["parts"]  # type: ignore[index]
        text = "".join(part.get("text", "") for part in parts).strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise LLMError("no text in response") from None
    if not text:
        raise LLMError("empty response")
    return text


class GeminiClient:
    """Talks to Gemini's REST API with httpx. The key travels in a header, never in the URL.

    Written from Google's REST docs and NOT exercised by the unit tests (they use a fake client).
    scripts/smoke_gemini.py is how we check it for real.
    """

    def __init__(self, api_key: str, *, max_output_tokens: int = 120, temperature: float = 0.8,
                 http: httpx.Client | None = None):
        self._key = api_key
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._http = http or httpx.Client()

    def __repr__(self) -> str:
        return "GeminiClient(api_key=***)"

    def generate_raw(self, model_id: str, system: str, prompt: str, timeout_s: float) -> RawResponse:
        if not _SAFE_MODEL_ID.match(model_id):
            raise LLMError("bad model id")
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": self._temperature, "maxOutputTokens": self._max_output_tokens},
        }
        headers = {"x-goog-api-key": self._key, "Content-Type": "application/json"}
        try:
            r = self._http.post(GEMINI_URL.format(model=model_id), headers=headers, json=body, timeout=timeout_s)
        except httpx.TimeoutException as e:
            raise LLMTimeout(f"timeout: {type(e).__name__}") from None
        except httpx.HTTPError as e:
            raise LLMError(f"network error: {type(e).__name__}") from None  # type only: no URL, no details
        try:
            parsed = r.json()
        except ValueError:
            parsed = None
        return RawResponse(r.status_code, scrub(r.text, [self._key]), parsed if isinstance(parsed, dict) else None)

    def generate(self, model_id: str, system: str, prompt: str, timeout_s: float) -> str:
        raw = self.generate_raw(model_id, system, prompt, timeout_s)
        if raw.status == 429:
            raise RateLimitError(raw.text)
        if raw.status != 200:
            raise LLMError(f"HTTP {raw.status}")
        return extract_text(raw.json)


def load_api_key() -> str:
    """Read GEMINI_API_KEY from .env. Reports nothing about its value."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    return os.environ.get("GEMINI_API_KEY", "").strip()


# --------------------------------------------------------------------------- the daily counter on disk

def atomic_write_text(path: Path, text: str, *, replace: Callable = os.replace,
                      sleep: Callable[[float], None] = time.sleep, attempts: int = 5, delay_s: float = 0.05) -> None:
    """Write a file so that a crash leaves either the old content or the new, never half of it.

    Write to a temporary file next to the target, force it to disk, then swap it in with os.replace
    (os.rename fails on Windows when the target exists). Antivirus can briefly lock the file, so a
    PermissionError is retried a few times.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(attempts):
        try:
            replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            sleep(delay_s * (attempt + 1))


def _fresh_state(day: str, blocked_all: bool = False) -> dict:
    return {"day": day, "counts": {}, "spent": [], "blocked_all": blocked_all}


def _valid_state(state: object) -> bool:
    return (
        isinstance(state, dict)
        and isinstance(state.get("day"), str)
        and isinstance(state.get("counts"), dict)
        and all(isinstance(v, int) and v >= 0 for v in state["counts"].values())
        and isinstance(state.get("spent"), list)
        and isinstance(state.get("blocked_all"), bool)
    )


class UsageStore:
    """How many requests each model has used today, saved in a JSON file.

    Every change is written to disk before the caller is told yes. One lock makes the
    check-and-increment a single step, so threads cannot both take the last slot.
    """

    def __init__(self, path: Path, *, reset_timezone: str, reset_hour: int,
                 wall: Callable[[], datetime] | None = None, writer: Callable[[Path, str], None] = atomic_write_text):
        self._path = path
        self._tz = ZoneInfo(reset_timezone)
        self._reset_hour = reset_hour
        self._wall = wall or (lambda: datetime.now(timezone.utc))
        self._writer = writer
        self._lock = threading.Lock()   # plain Lock on purpose: tests can tell whether it is held
        self._state = self._load()

    def _today(self) -> str:
        local = self._wall().astimezone(self._tz) - timedelta(hours=self._reset_hour)
        return local.date().isoformat()

    def _save(self) -> None:
        self._writer(self._path, json.dumps(self._state, indent=2))

    def _load(self) -> dict:
        today = self._today()
        if not self._path.exists():
            return _fresh_state(today)
        try:
            state = json.loads(self._path.read_text(encoding="utf-8"))
            if not _valid_state(state):
                raise ValueError("unexpected shape")
        except (OSError, ValueError) as e:
            corrupt = self._path.with_name(self._path.name + ".corrupt")
            log.error("LLM USAGE FILE IS CORRUPT (%s). Keeping a copy at %s and blocking Gemini for the rest of "
                      "the day, because the real counts are unknown. Templates keep working.", e, corrupt)
            try:
                os.replace(self._path, corrupt)
            except OSError:
                log.error("could not move the corrupt file aside; it will be overwritten")
            state = _fresh_state(today, blocked_all=True)
            self._state = state
            try:
                self._save()
            except OSError:
                log.error("could not save the 'blocked for today' marker; the block holds only until restart")
            return state
        return state if state["day"] == today else _fresh_state(today)

    def _roll_if_needed(self) -> None:
        today = self._today()
        if self._state["day"] != today:
            self._state = _fresh_state(today)
            try:
                self._save()
            except OSError:
                log.error("could not save the new day's usage file")

    def try_reserve(self, model_id: str, per_day: int) -> bool:
        """Take one slot for today. True means we may send a request; the slot is already saved."""
        with self._lock:
            self._roll_if_needed()
            state = self._state
            if state["blocked_all"] or model_id in state["spent"]:
                return False
            used = state["counts"].get(model_id, 0)
            if used >= per_day:
                return False
            state["counts"][model_id] = used + 1
            try:
                self._save()
            except OSError:
                state["counts"][model_id] = used
                log.error("could not save the usage file, so no request is sent")
                return False
            return True

    def mark_spent(self, model_id: str) -> None:
        with self._lock:
            self._roll_if_needed()
            if model_id not in self._state["spent"]:
                self._state["spent"].append(model_id)
            try:
                self._save()
            except OSError:
                log.error("could not save 'spent' for %s; it holds only until restart", model_id)

    def count(self, model_id: str) -> int:
        with self._lock:
            self._roll_if_needed()
            return self._state["counts"].get(model_id, 0)

    def is_spent(self, model_id: str) -> bool:
        with self._lock:
            self._roll_if_needed()
            return model_id in self._state["spent"]

    def is_blocked_all(self) -> bool:
        with self._lock:
            self._roll_if_needed()
            return self._state["blocked_all"]


# --------------------------------------------------------------------------- the router

@dataclass(frozen=True)
class LLMResult:
    text: str
    model_id: str


class LLMRouter:
    def __init__(self, config: LLMConfig, client: LLMClient | None, store: UsageStore, *,
                 secrets: Iterable[str] = (), monotonic: Callable[[], float] = time.monotonic):
        self._config = config
        self._client = client
        self._store = store
        self._secrets = [s for s in secrets if s]
        self._mono = monotonic
        self._lock = threading.Lock()                       # guards the two dicts below, never held during a request
        self._windows: dict[str, deque[float]] = {}         # request times in the last minute, per model
        self._backoff: dict[str, tuple[float, float]] = {}  # model -> (rest until, last delay)
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")

    # -- public

    def complete(self, system: str, prompt: str) -> LLMResult | None:
        """Try the models in order. Returns None when nothing answered in time: use a template."""
        if self._client is None:
            return None
        cfg = self._config
        start = self._mono()
        tried = 0
        for model in cfg.models:
            if tried >= cfg.max_models_per_turn:
                break
            remaining = cfg.overall_deadline_s - (self._mono() - start)
            if remaining <= 0.05:
                break
            if model.is_placeholder or not self._reserve(model):
                continue
            tried += 1
            try:
                text = self._call(model, system, prompt, min(cfg.request_timeout_s, remaining))
            except RateLimitError as e:
                self._on_429(model, e)
            except LLMError as e:
                log.warning("%s failed: %s", model.label, scrub(str(e), self._secrets))
                self._start_backoff(model)
            else:
                self._clear_backoff(model)
                return LLMResult(text, model.model_id)
        return None

    async def acomplete(self, system: str, prompt: str) -> LLMResult | None:
        """The same, run in a worker thread so disk writes and HTTP never block the live audio loop."""
        return await asyncio.to_thread(self.complete, system, prompt)

    # -- internals

    def _reserve(self, model: ModelBudget) -> bool:
        """Check rest period and per-minute window, then take a daily slot. All in one locked step."""
        now = self._mono()
        with self._lock:
            if now < self._backoff.get(model.model_id, (0.0, 0.0))[0]:
                return False
            window = self._windows.setdefault(model.model_id, deque())
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= model.per_minute:
                return False
            if not self._store.try_reserve(model.model_id, model.per_day):
                return False
            window.append(now)
            return True

    def _call(self, model: ModelBudget, system: str, prompt: str, timeout_s: float) -> str:
        """Run the client in a worker so a hung request cannot hold us past the deadline."""
        future = self._executor.submit(self._client.generate, model.model_id, system, prompt, timeout_s)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout:
            future.cancel()
            raise LLMError("deadline passed") from None
        except (RateLimitError, LLMError):
            raise
        except Exception as e:  # a client bug must not crash a live call
            raise LLMError(f"client error: {type(e).__name__}") from None

    def _on_429(self, model: ModelBudget, error: RateLimitError) -> None:
        self._log_first_429(model, error.body)
        kind = classify_429(error.body)
        if kind == "daily":
            log.warning("%s: daily quota used up; resting until the reset", model.label)
            self._store.mark_spent(model.model_id)
        else:
            log.warning("%s: 429 (%s); backing off", model.label, kind)
            self._start_backoff(model)

    def _start_backoff(self, model: ModelBudget) -> None:
        cfg = self._config
        with self._lock:
            previous = self._backoff.get(model.model_id, (0.0, 0.0))[1]
            delay = min(previous * 2, cfg.backoff_max_s) if previous else cfg.backoff_start_s
            self._backoff[model.model_id] = (self._mono() + delay, delay)

    def _clear_backoff(self, model: ModelBudget) -> None:
        with self._lock:
            self._backoff.pop(model.model_id, None)

    def _log_first_429(self, model: ModelBudget, body: str) -> None:
        """Keep the raw text of the very first 429 we ever see (mode "x" = create, never overwrite)."""
        text = f"{datetime.now(timezone.utc).isoformat()} model={model.model_id}\n{scrub(body, self._secrets)[:4000]}\n"
        try:
            self._config.first_429_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._config.first_429_path, "x", encoding="utf-8") as f:
                f.write(text)
        except FileExistsError:
            pass
        except OSError:
            log.error("could not save the first 429 text")


def build_router(config: LLMConfig | None = None, api_key: str | None = None) -> LLMRouter:
    """The usual way to get a router. With no key it still works and always answers None."""
    config = config or default_config()
    key = load_api_key() if api_key is None else api_key
    if not key:
        log.warning("GEMINI_API_KEY is missing: every LLM call will return None and templates will be used")
    client = GeminiClient(key, max_output_tokens=config.max_output_tokens, temperature=config.temperature) if key else None
    store = UsageStore(config.usage_path, reset_timezone=config.reset_timezone, reset_hour=config.reset_hour)
    return LLMRouter(config, client, store, secrets=[key])
