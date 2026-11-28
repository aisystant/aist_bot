"""Counts failed attempts per key (for example per client address) in a sliding window.

In memory and per process: enough to keep guessing of an API key and the load it
causes down; it restarts empty on every deploy.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable

_MAX_TRACKED_KEYS = 10_000


class FailureLimiter:
    def __init__(self, max_failures: int, window_sec: float, clock: Callable[[], float] = time.monotonic):
        self._max_failures = max_failures
        self._window_sec = window_sec
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, key: str) -> deque[float]:
        failures = self._failures.get(key)
        if failures is None:
            return deque()
        cutoff = self._clock() - self._window_sec
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            del self._failures[key]
        return failures

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self._max_failures

    def record(self, key: str) -> None:
        if len(self._failures) >= _MAX_TRACKED_KEYS and key not in self._failures:
            self._failures.clear()
        self._failures.setdefault(key, deque()).append(self._clock())
