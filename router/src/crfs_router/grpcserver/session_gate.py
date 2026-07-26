"""Per-session mutual exclusion.

A session maps to one paused container holding one Python process behind one
unix socket. The worker resumes it with Unpause followed by Update, and drives
it through a single executor session. Two concurrent requests for one session
would race on all three, and would interleave in one interpreter's global state
even if they did not.

The router is the only component that knows the session-to-container mapping,
so it is the only component that can prevent this.

This lives in the transport layer rather than beside the scheduler on purpose:
it is the one asyncio primitive on the request path, and putting it next to the
scheduler would invite someone to await inside a domain object whose atomicity
depends on never awaiting.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
from collections.abc import AsyncIterator

from crfs_router.errors import SessionBusyError


class SessionGate:
    """Serialises the requests belonging to one session.

    The lock is deliberately held across awaits, for the whole lifetime of a
    request. It is a queue, not a critical section.
    """

    def __init__(self, *, wait_timeout: float) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._waiters: Counter[str] = Counter()
        self._wait_timeout = wait_timeout

    @contextlib.asynccontextmanager
    async def hold(self, session_id: str) -> AsyncIterator[None]:
        """Take this session's turn, waiting if another request holds it.

        Raises:
            SessionBusyError: the wait exceeded the configured timeout.
        """
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        self._waiters[session_id] += 1
        try:
            async with asyncio.timeout(self._wait_timeout):
                await lock.acquire()
        except TimeoutError as exc:
            raise SessionBusyError(session_id, waited=self._wait_timeout) from exc
        finally:
            self._waiters[session_id] -= 1

        try:
            yield
        finally:
            lock.release()
            # Dropping the entry is what keeps this dict bounded across a long
            # run. Safe here only because nothing yields between the check and
            # the delete.
            if self._waiters[session_id] == 0 and not lock.locked():
                self._locks.pop(session_id, None)
                del self._waiters[session_id]

    def tracked_sessions(self) -> int:
        """Return how many sessions currently hold an entry.

        Exposed for tests: an entry surviving after every request has finished
        is a leak.
        """
        return len(self._locks)
