"""Session affinity and the serialisation it forces.

A session maps to one paused container holding one Python process behind one
socket. Reusing it is the point of the system; letting two requests into it at
once would corrupt it.
"""

from __future__ import annotations

import asyncio

import grpc
import pytest

from tests.integration.conftest import Harness
from tests.integration.test_proxy_service import chunk, execute, header, stream_of


async def test_a_second_call_in_a_session_reuses_the_container(harness: Harness) -> None:
    await harness.register_worker()

    await execute(harness, header("req-1", "sess-1"), chunk(b"a", "req-1", "sess-1"))
    await execute(harness, header("req-2", "sess-1"), chunk(b"b", "req-2", "sess-1"))

    first, second = harness.worker.calls
    assert first.header.container_id == "", "the first call cold-starts"
    assert second.header.container_id != "", "the second must resume the warm container"


async def test_different_sessions_do_not_share_a_container(harness: Harness) -> None:
    await harness.register_worker()

    await execute(harness, header("req-1", "sess-1"), chunk(b"a", "req-1", "sess-1"))
    await execute(harness, header("req-2", "sess-2"), chunk(b"b", "req-2", "sess-2"))

    assert all(call.header.container_id == "" for call in harness.worker.calls), (
        "an unrelated session must never be handed another session's container"
    )


async def test_concurrent_calls_in_one_session_are_serialised(harness: Harness) -> None:
    """The core safety property.

    One container is one Python interpreter behind one socket; the worker
    resumes it with Unpause+Update. Two concurrent executions would race on all
    three, so the router queues them.
    """
    harness.worker.response_delay = 0.3
    await harness.register_worker()

    async def call(request_id: str) -> None:
        await execute(
            harness,
            header(request_id, "sess-1"),
            chunk(b"x", request_id, "sess-1"),
        )

    await asyncio.gather(call("req-1"), call("req-2"))

    assert len(harness.worker.calls) == 2
    # If they had overlapped, the second would have been placed before the
    # first bound its container and would also have cold-started.
    assert harness.worker.calls[1].header.container_id != "", (
        "the second call ran after the first, so it must have seen the warm container"
    )


async def test_concurrent_calls_in_different_sessions_are_not_serialised(
    harness: Harness,
) -> None:
    """Serialisation is per session; unrelated work must stay parallel."""
    harness.worker.response_delay = 0.3
    await harness.register_worker()

    async def call(session_id: str) -> None:
        await execute(
            harness,
            header("req-" + session_id, session_id),
            chunk(b"x", "req-" + session_id, session_id),
        )

    started = asyncio.get_running_loop().time()
    await asyncio.gather(call("sess-1"), call("sess-2"))
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.55, f"two sessions should overlap, took {elapsed:.2f}s"


async def test_a_failed_call_does_not_pin_the_session(harness: Harness) -> None:
    """A cold start that failed leaves no container; a later call must not wait."""
    await harness.register_worker()
    harness.worker.error = grpc.aio.AioRpcError(
        code=grpc.StatusCode.INTERNAL,
        initial_metadata=grpc.aio.Metadata(),
        trailing_metadata=grpc.aio.Metadata(),
        details="boom",
    )

    with pytest.raises(grpc.aio.AioRpcError):
        await execute(harness, header("req-1", "sess-1"), chunk(b"x", "req-1", "sess-1"))

    session = harness.app.state.session("sess-1")
    assert session is not None
    assert session.affinity is None, (
        "a failed placement must not leave the session pinned to a container "
        "that may never have existed"
    )

    harness.worker.error = None
    await execute(harness, header("req-2", "sess-1"), chunk(b"y", "req-2", "sess-1"))
    assert harness.worker.calls[-1].header.container_id == "", "it must cold-start again"


async def test_a_cancelled_call_releases_the_session(harness: Harness) -> None:
    harness.worker.response_delay = 10.0
    await harness.register_worker()

    call = harness.proxy.RequestExecution(
        stream_of(header("req-1", "sess-1"), chunk(b"x", "req-1", "sess-1"))
    )
    await asyncio.sleep(0.2)
    call.cancel()
    await asyncio.sleep(0.3)

    harness.worker.response_delay = 0.0
    # If the gate had not been released this would block until the wait timeout.
    await asyncio.wait_for(
        execute(harness, header("req-2", "sess-1"), chunk(b"y", "req-2", "sess-1")),
        timeout=3.0,
    )
