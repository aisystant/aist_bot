"""HTTP transport: hard time limits, backoff with jitter, "unknown outcome" rule.

Rules (WP-568 F17 item 5, agreed by writer + Kimi, checked by an independent review):
- every attempt has a hard timeout; the whole call has one time budget that
  includes the time spent inside the calls themselves;
- a rejected-before-processing answer (HTTP 429) is always safe to repeat;
- any other uncertain end (timeout, 5xx, connection cut, unreadable body) is
  repeated only for calls marked `safe_to_retry`; a mutating call returns
  OUTCOME_UNKNOWN with a reconcile key and is never repeated blindly.

All numbers in AdapterConfig are STARTING values: only single-call latencies
(0.3-2 s, 1000 people in 14 s) were measured on 2026-11-18. Rate limits, the
presence of a Retry-After header and timeouts per call type are tuned by the live check.
"""
from __future__ import annotations

import http.client
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Optional, Tuple

from .models import Outcome, Result

logger = logging.getLogger(__name__)

Opener = Callable[[urllib.request.Request, float], Tuple[int, Dict[str, str], bytes]]


@dataclass(frozen=True)
class AdapterConfig:
    base_url: str = "https://api.knock.app/v1"
    call_timeout: float = 10.0       # single-person operations
    batch_timeout: float = 30.0      # bulk operations
    max_timeout: float = 60.0        # ceiling for any configured timeout
    total_budget: float = 90.0       # whole call, including time inside the calls
    max_attempts: int = 5
    pause_base: float = 1.0
    pause_multiplier: float = 2.0
    pause_max: float = 30.0          # not reached with 5 attempts (1-2-4-8 s); for larger max_attempts
    jitter: float = 0.25
    retry_after_cap: float = 60.0
    max_concurrency: int = 4         # calls in flight over the whole adapter
    bulk_concurrency: int = 2        # parallel chunks of one bulk operation
    bulk_chunk_size: int = 100       # people per bulk call (trial: 100 per call)
    poll_interval: float = 2.0       # wait_people_ready
    future_gap_horizon_days: int = 730
    # ASSUMPTION, verify live: creating a person by our id is safe to repeat.
    person_upsert_idempotent: bool = True
    # Flip to True only after the live check confirms Knock honours an idempotency key.
    trigger_idempotency_verified: bool = False

    def __post_init__(self) -> None:
        if urllib.parse.urlparse(self.base_url).scheme not in ("http", "https"):
            raise ValueError("base_url must use http or https")  # no file:/ or custom schemes
        for name in ("call_timeout", "batch_timeout"):
            value = getattr(self, name)
            if not 0 < value <= self.max_timeout:
                raise ValueError("{} must be in (0, max_timeout]".format(name))
        if self.max_attempts < 1 or self.total_budget <= 0:
            raise ValueError("max_attempts >= 1 and total_budget > 0 required")
        if self.bulk_concurrency > self.max_concurrency or self.bulk_concurrency < 1:
            raise ValueError("1 <= bulk_concurrency <= max_concurrency required")


def default_opener(request: urllib.request.Request, timeout: float):
    try:
        # nosec B310: the scheme is restricted to http/https in AdapterConfig.__post_init__
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


@dataclass(frozen=True)
class _Attempt:
    kind: str  # ok | not_found | rate | unknown | permanent
    status: Optional[int] = None
    data: Optional[Dict[str, Any]] = None
    reason: str = ""
    retry_after: Optional[float] = None


def _parse_retry_after(headers: Dict[str, str]) -> Optional[float]:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                return None  # HTTP-date form is not supported on purpose
            return seconds if seconds >= 0 else None
    return None


class KnockTransport:
    def __init__(
        self,
        api_key: str,
        config: AdapterConfig,
        *,
        opener: Opener = default_opener,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required (runtime key only, never the management key)")
        self._key = api_key
        self._cfg = config
        self._opener = opener
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._slots = threading.BoundedSemaphore(config.max_concurrency)

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        safe_to_retry: bool,
        batch: bool = False,
        not_found_ok: bool = False,
        headers: Optional[Dict[str, str]] = None,
        reconcile_key: Optional[Tuple[str, str, str]] = None,
    ) -> Result:
        cfg = self._cfg
        per_call = cfg.batch_timeout if batch else cfg.call_timeout
        started = self._clock()
        attempts = 0
        outcome: Optional[_Attempt] = None
        while True:
            remaining = cfg.total_budget - (self._clock() - started)
            if outcome is not None and remaining <= 0:
                # A real sleep can overshoot; never start an attempt with no budget left.
                return self._exhausted(outcome, attempts, reconcile_key)
            attempts += 1
            outcome = self._attempt(method, path, body, headers, min(per_call, max(remaining, 0.001)))
            done = self._finish(outcome, attempts, not_found_ok, safe_to_retry, reconcile_key)
            if done is not None:
                return done
            pause = self._pause(attempts, outcome)
            remaining = cfg.total_budget - (self._clock() - started)
            if attempts >= cfg.max_attempts or pause >= remaining:
                return self._exhausted(outcome, attempts, reconcile_key)
            logger.info("[knock_adapter] retry %s %s attempt=%d reason=%s pause=%.1fs",
                        method, path, attempts, outcome.reason, pause)
            self._sleep(pause)

    def _attempt(self, method, path, body, headers, timeout) -> _Attempt:
        try:
            payload = None if body is None else json.dumps(body).encode("utf-8")
            request = urllib.request.Request(self._cfg.base_url + path, data=payload, method=method)
            request.add_header("Authorization", "Bearer " + self._key)
            request.add_header("Content-Type", "application/json")
            for name, value in (headers or {}).items():
                request.add_header(name, value)
        except (TypeError, ValueError) as error:
            # Nothing was sent: unserializable body or malformed URL. Final, not repeatable.
            logger.warning("[knock_adapter] invalid request %s %s: %s", method, path, type(error).__name__)
            return _Attempt("permanent", reason="invalid_request")
        with self._slots:
            try:
                status, response_headers, raw = self._opener(request, timeout)
            except TimeoutError:
                return _Attempt("unknown", reason="timeout")
            except (OSError, http.client.HTTPException) as error:
                if "timed out" in str(error).lower():
                    return _Attempt("unknown", reason="timeout")
                return _Attempt("unknown", reason="connection_error")
        return self._classify(status, response_headers, raw)

    @staticmethod
    def _classify(status: int, headers: Dict[str, str], raw: bytes) -> _Attempt:
        if 200 <= status < 300:
            if not raw:
                return _Attempt("ok", status, {})
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return _Attempt("unknown", status, reason="invalid_json")
            return _Attempt("ok", status, parsed if isinstance(parsed, dict) else {"items": parsed})
        if status == 404:
            return _Attempt("not_found", status, reason="not_found")
        if status == 429:
            return _Attempt("rate", status, reason="rate_limited", retry_after=_parse_retry_after(headers))
        if status >= 500:
            return _Attempt("unknown", status, reason="http_{}".format(status))
        return _Attempt("permanent", status, reason="http_{}".format(status))

    def _finish(self, attempt, attempts, not_found_ok, safe_to_retry, reconcile_key) -> Optional[Result]:
        """Return a final Result, or None when the call must be repeated."""
        kind = attempt.kind
        if kind == "ok":
            return Result(Outcome.OK, status_code=attempt.status, data=attempt.data, attempts=attempts)
        if kind == "not_found":
            if not_found_ok:
                return Result(Outcome.OK, "already_absent", attempt.status, {}, attempts)
            return Result(Outcome.PERMANENT_ERROR, "not_found", attempt.status, attempts=attempts)
        if kind == "permanent":
            logger.warning("[knock_adapter] permanent error status=%s", attempt.status)
            return Result(Outcome.PERMANENT_ERROR, attempt.reason, attempt.status, attempts=attempts)
        if kind == "unknown" and not safe_to_retry:
            logger.warning("[knock_adapter] outcome unknown reason=%s key=%s", attempt.reason, reconcile_key)
            return Result(Outcome.OUTCOME_UNKNOWN, attempt.reason, attempt.status, attempts=attempts,
                          reconcile_key=reconcile_key)
        return None

    def _pause(self, attempts: int, attempt: _Attempt) -> float:
        cfg = self._cfg
        if attempt.retry_after is not None:
            return min(attempt.retry_after, cfg.retry_after_cap)
        pause = min(cfg.pause_max, cfg.pause_base * cfg.pause_multiplier ** (attempts - 1))
        return pause * (1.0 + cfg.jitter * self._rng.uniform(-1.0, 1.0))

    @staticmethod
    def _exhausted(attempt: _Attempt, attempts: int, reconcile_key) -> Result:
        logger.warning("[knock_adapter] gave up reason=%s attempts=%d", attempt.reason, attempts)
        if attempt.kind == "unknown" and attempt.reason == "timeout":
            return Result(Outcome.TIMEOUT, "timeout", attempt.status, attempts=attempts)
        return Result(Outcome.RETRYABLE_ERROR, attempt.reason, attempt.status, attempts=attempts)


def with_warnings(result: Result, warnings: Tuple[str, ...]) -> Result:
    return replace(result, warnings=tuple(result.warnings) + tuple(warnings))
