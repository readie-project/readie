"""Keep calls to a rate-limited service under its limit, and wait out its pushback.

Fixed retries are the wrong tool for sustained throttling. Seen live: Kaggle
answered 429 for over half an hour, every download backed off for about thirty
seconds and then reported nothing, so a pair that needed 119 items sat idle for
forty minutes with no error logged. The :class:`Pacer` adapts instead. Every call
is spaced from the last, the spacing doubles each time the service pushes back
and eases off while calls succeed, and a call that is still refused keeps waiting,
out loud, for minutes before it gives up.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

_T = TypeVar("_T")

#: Statuses worth waiting out. Only 429 means "slow down", so only it widens the spacing.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RATE_LIMITED = 429
#: Waits at or above this many seconds are reported, so a long stall is visible.
_REPORT_FROM = 20.0


def http_status(exc: BaseException) -> int | None:
    """The HTTP status an exception carries, or ``None`` when it carries none."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


class Pacer:
    """Spaces calls to one service and retries the ones it refuses."""

    def __init__(
        self,
        *,
        base_interval: float = 0.3,
        max_interval: float = 30.0,
        attempts: int = 8,
        base_backoff: float = 5.0,
        max_backoff: float = 120.0,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> None:
        self._base_interval = base_interval
        self._max_interval = max_interval
        self._attempts = attempts
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        # Resolved here, not as default arguments, so a test can patch time.sleep.
        self._sleep = sleep if sleep is not None else time.sleep
        self._clock = clock if clock is not None else time.monotonic
        self._on_line = on_line
        self.interval = base_interval
        self._last_call: float | None = None
        # Several threads share one pacer, so the spacing is handed out under a lock.
        self._lock = threading.Lock()

    def call(self, fn: Callable[[], _T]) -> _T:
        """Run ``fn`` paced, retrying 429 and server errors with growing waits.

        Raises:
            Exception: Whatever ``fn`` raised, once it is not worth retrying or the
                attempts are used up.
        """
        for attempt in range(1, self._attempts + 1):
            self._space()
            try:
                result = fn()
            except Exception as exc:
                status = http_status(exc)
                if status not in RETRY_STATUSES or attempt == self._attempts:
                    raise
                if status == _RATE_LIMITED:
                    with self._lock:
                        self.interval = min(self._max_interval, self.interval * 2)
                self._wait(attempt, status)
            else:
                with self._lock:
                    self.interval = max(self._base_interval, self.interval * 0.95)
                return result
        msg = "retry loop ended without a result"
        raise AssertionError(msg)  # pragma: no cover

    def _space(self) -> None:
        """Reserve the next start time, at least ``interval`` after the last, and wait for it.

        The reservation is made under the lock and the waiting outside it, so
        threads calling together are spread out instead of queueing on the lock.
        """
        with self._lock:
            now = self._clock()
            start = now if self._last_call is None else max(now, self._last_call + self.interval)
            self._last_call = start
        if start > now:
            self._sleep(start - now)

    def _wait(self, attempt: int, status: Any) -> None:
        delay = min(self._max_backoff, self._base_backoff * 2 ** (attempt - 1))
        if self._on_line is not None and delay >= _REPORT_FROM:
            self._on_line(
                f"    service answered {status}; waiting {delay:.0f}s "
                f"(attempt {attempt} of {self._attempts})"
            )
        self._sleep(delay)
