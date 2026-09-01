"""Time, behind a seam.

Every TTL and lease deadline reads through this, so the reaper and the prober
can be tested by advancing a fake rather than by sleeping.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """A source of monotonic time."""

    def now(self) -> float:
        """Seconds from an arbitrary origin.

        Monotonic, never wall-clock: a TTL must survive an NTP step, and a
        clock that can jump backwards makes a lease look eternally young.
        """
        ...


class MonotonicClock:
    """The real clock."""

    def now(self) -> float:
        """Return the current monotonic time in seconds."""
        return time.monotonic()


class FakeClock:
    """A manually advanced clock, for tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        """Return the current fake time."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move time forward."""
        self._now += seconds
