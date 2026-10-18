"""Minimal Gemini client: structured JSON output, free-tier rate limiting.

Free tier has per-minute request limits that are not published per model and
change without notice, so this paces itself and backs off on 429 rather than
assuming a number.
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# The free tier grants a small DAILY request quota per model, measured as
# GenerateRequestsPerDayPerProjectPerModel-FreeTier. It was 20/day for
# gemini-3.8-flash when this was built. Quotas are per model, so rotating
# through a pool multiplies the daily ceiling.
#
# Ordered best first. Failover walks down the list. "gemini-flash-latest" is
# deliberately absent: it aliases 3.8-flash and shares its quota.
MODEL_POOL = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemma-4-26b-a4b-it",
]
DEFAULT_MODEL = MODEL_POOL[0]

# Models known to be out of daily quota, and when to reconsider them.
_EXHAUSTED: dict[str, float] = {}
_EXHAUSTED_LOCK = threading.Lock()
# Quota resets are daily; re-probe well before that in case it was a
# per-minute limit misread as a daily one.
_RETRY_AFTER_EXHAUSTION = 1800.0


def available_models(pool: list[str] | None = None) -> list[str]:
    pool = pool or MODEL_POOL
    now = time.monotonic()
    with _EXHAUSTED_LOCK:
        return [m for m in pool if _EXHAUSTED.get(m, 0.0) <= now]


def mark_exhausted(model: str, seconds: float = _RETRY_AFTER_EXHAUSTION) -> None:
    with _EXHAUSTED_LOCK:
        _EXHAUSTED[model] = time.monotonic() + seconds


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


load_env()


class RateLimiter:
    """Token bucket with adaptive backoff. Shared across threads."""

    def __init__(self, per_minute: int = 12):
        self.interval = 60.0 / per_minute
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.penalty = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            target = max(now, self.next_at) + self.penalty
            self.next_at = target + self.interval
            delay = target - now
        if delay > 0:
            time.sleep(delay)

    def throttled(self) -> None:
        """Called on a 429. Widens the gap for everything that follows."""
        with self.lock:
            self.penalty = min(self.penalty * 2 + 1.0, 30.0)

    def ok(self) -> None:
        with self.lock:
            self.penalty = max(0.0, self.penalty * 0.5 - 0.1)


LIMITER = RateLimiter()


class GeminiError(RuntimeError):
    pass


def _extract_text(payload: dict) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise GeminiError(f"no candidates: {json.dumps(payload)[:300]}")
    cand = candidates[0]
    finish = cand.get("finishReason")
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if not text:
        raise GeminiError(f"empty response, finishReason={finish}")
    return text


DAILY_QUOTA_RE = re.compile(r"PerDay|RequestsPerDay", re.I)


def _parse_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Structured mode occasionally wraps JSON in a fence.
        m = re.search(r"[\{\[].*[\}\]]", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise GeminiError(f"unparseable JSON: {text[:300]}")


def _call(model: str, body: dict, key: str) -> tuple[int, dict | None, str]:
    LIMITER.wait()
    r = requests.post(
        f"{BASE}/{model}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json=body,
        timeout=(15, 90),
    )
    if r.status_code == 200:
        LIMITER.ok()
        return 200, r.json(), ""
    return r.status_code, None, r.text[:600]


def generate(
    prompt: str,
    *,
    schema: dict | None = None,
    model: str | None = None,
    models: list[str] | None = None,
    max_tokens: int = 8000,
    temperature: float = 0.4,
    system: str | None = None,
    retries: int = 3,
) -> dict | str:
    """One generation call, with automatic failover across the model pool.

    When a model's daily free-tier quota is exhausted it is benched and the
    next model in the pool is tried. Pass `model` to pin one, or `models` to
    supply a custom preference order.
    """
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise GeminiError("GEMINI_API_KEY not set in .env")

    pool = [model] if model else available_models(models)
    if not pool:
        pool = [(models or MODEL_POOL)[-1]]  # all benched; try the cheapest anyway

    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if schema:
        body["generationConfig"]["responseMimeType"] = "application/json"
        body["generationConfig"]["responseSchema"] = schema

    last = "no attempt made"

    for candidate in pool:
        for attempt in range(retries):
            try:
                status, payload, err = _call(candidate, body, key)
            except requests.Timeout:
                # A model that hangs burns far more wall-clock than one that
                # refuses: three retries at the request timeout each. Bench it
                # and move to the next model instead of waiting it out.
                mark_exhausted(candidate, 900)
                last = f"{candidate}: timed out"
                break
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(2 ** attempt + random.random())
                continue

            if status == 200:
                text = _extract_text(payload)
                return _parse_json(text) if schema else text

            if status == 429:
                LIMITER.throttled()
                # A daily quota will not clear by waiting a few seconds, so
                # bench the model and move on instead of burning retries.
                if DAILY_QUOTA_RE.search(err):
                    mark_exhausted(candidate)
                    last = f"{candidate}: daily quota exhausted"
                    break
                m = re.search(r"retry in ([\d.]+)s", err)
                time.sleep(min(float(m.group(1)) if m else 2 ** attempt + 1, 65))
                last = f"{candidate}: rate limited"
                continue

            if status >= 500:
                last = f"{candidate}: {status} server error"
                time.sleep(2 ** attempt + random.random())
                continue

            if status == 404:
                mark_exhausted(candidate, 86400)
                last = f"{candidate}: not available"
                break

            raise GeminiError(f"{status}: {err}")

    raise GeminiError(f"all models failed. last: {last}")
