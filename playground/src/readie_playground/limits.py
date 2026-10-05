"""Abuse controls Readie does not provide: a per-client rate limit and a run cap."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    """Sliding-window limiter keyed by client address."""

    def __init__(
        self,
        max_requests: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_requests
        self._window = window_s
        self._clock = clock
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        """Record a request for ``key`` and return whether it is within the limit."""
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if not hits:
            # Drop idle keys so the table does not grow with every address seen.
            self._hits.pop(key, None)
            hits = self._hits[key]
        if len(hits) >= self._max:
            return False
        hits.append(now)
        return True


class RunSlots:
    """A non-blocking cap on simultaneous runs.

    Each run holds two 4 GiB sandboxes, so the cap protects worker memory. A full
    gate rejects instead of queueing, which keeps latency honest.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0

    def try_acquire(self) -> bool:
        """Take a slot if one is free."""
        if self._active >= self._limit:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        """Give a slot back."""
        self._active = max(0, self._active - 1)
