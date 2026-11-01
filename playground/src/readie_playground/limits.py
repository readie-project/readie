"""A cap on concurrent public execution requests."""

from __future__ import annotations


class RunSlots:
    """A non-blocking cap on simultaneous runs.

    A full gate rejects instead of queueing. This bounds in-flight HTTP requests;
    the router and workers handle execution resource allocation.
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
