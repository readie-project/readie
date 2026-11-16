"""Abuse control Readie does not provide: a per-client rate limit."""

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
