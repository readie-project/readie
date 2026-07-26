"""Per-session mutual exclusion.

The gate is the only asyncio primitive on the request path, and it is what
stops two requests entering one container. Its failure modes are a deadlock and
a slow leak, so both are tested directly rather than inferred from the relay.
"""

from __future__ import annotations

import asyncio

import pytest

from crfs_router.errors import SessionBusyError
from crfs_router.grpcserver.session_gate import SessionGate


async def test_one_session_admits_one_holder_at_a_time() -> None:
    gate = SessionGate(wait_timeout=5.0)
    order: list[str] = []

    async def hold(name: str, duration: float) -> None:
        async with gate.hold("sess-1"):
            order.append(f"{name}:enter")
            await asyncio.sleep(duration)
            order.append(f"{name}:exit")

    await asyncio.gather(hold("a", 0.1), hold("b", 0.0))

    # Whoever went first must have finished before the other started.
    assert order in (
        ["a:enter", "a:exit", "b:enter", "b:exit"],
        ["b:enter", "b:exit", "a:enter", "a:exit"],
    ), order


async def test_different_sessions_do_not_block_each_other() -> None:
    gate = SessionGate(wait_timeout=5.0)

    async def hold(session_id: str) -> None:
        async with gate.hold(session_id):
            await asyncio.sleep(0.2)

    started = asyncio.get_running_loop().time()
    await asyncio.gather(hold("sess-1"), hold("sess-2"))
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.35, f"unrelated sessions must overlap, took {elapsed:.2f}s"


async def test_waiting_too_long_raises_rather_than_queueing_forever() -> None:
    """A stuck session must shed load, not accumulate waiters indefinitely."""
    gate = SessionGate(wait_timeout=0.1)
    holding = asyncio.Event()

    async def hold_forever() -> None:
        async with gate.hold("sess-1"):
            holding.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(hold_forever())
    await holding.wait()

    with pytest.raises(SessionBusyError):
        async with gate.hold("sess-1"):
            pass  # pragma: no cover - the wait must not succeed

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_entries_are_released_so_the_map_stays_bounded() -> None:
    """A long-running router would otherwise accumulate one lock per session."""
    gate = SessionGate(wait_timeout=5.0)

    for i in range(100):
        async with gate.hold(f"sess-{i}"):
            pass

    assert gate.tracked_sessions() == 0


async def test_an_entry_survives_while_a_waiter_is_queued() -> None:
    """Dropping the entry with a waiter attached would lose the queue."""
    gate = SessionGate(wait_timeout=5.0)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with gate.hold("sess-1"):
            holding.set()
            await release.wait()

    async def waiter() -> None:
        async with gate.hold("sess-1"):
            pass

    held = asyncio.create_task(holder())
    await holding.wait()
    queued = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)

    assert gate.tracked_sessions() == 1

    release.set()
    await asyncio.gather(held, queued)
    assert gate.tracked_sessions() == 0


async def test_an_exception_inside_the_hold_still_releases_it() -> None:
    gate = SessionGate(wait_timeout=1.0)

    with pytest.raises(ValueError, match="boom"):
        async with gate.hold("sess-1"):
            raise ValueError("boom")

    # If the lock had leaked this would raise SessionBusyError.
    async with gate.hold("sess-1"):
        pass
    assert gate.tracked_sessions() == 0
