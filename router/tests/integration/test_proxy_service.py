"""The client → router → worker bridge, over a real gRPC channel."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import grpc
import pytest

from readie_router.proto import execution_pb2, proxy_pb2, registry_pb2, resources_pb2
from tests.fakes.worker import FakeWorker
from tests.integration.conftest import Harness


def header(
    request_id: str = "req-1",
    session_id: str = "sess-1",
    *,
    imports: tuple[str, ...] = ("pandas", "numpy"),
    memory: int = 0,
    max_memory: int = 0,
) -> proxy_pb2.ClientExecutionRequest:
    """The first message a client must send."""
    budgets = []
    if memory or max_memory:
        budgets.append(
            resources_pb2.ResourceBudget(
                kind=resources_pb2.RESOURCE_KIND_MEMORY, alloc=memory, max=max_memory
            )
        )
    return proxy_pb2.ClientExecutionRequest(
        request_id=request_id,
        session_id=session_id,
        config=proxy_pb2.ExecutionConfig(
            imports=list(imports), budgets=budgets),
    )


def memory_budget(
    request: execution_pb2.WorkerExecutionRequest,
) -> resources_pb2.ResourceBudget | None:
    """The memory budget on a worker request, if any."""
    for budget in request.budgets:
        if budget.kind == resources_pb2.RESOURCE_KIND_MEMORY:
            return budget
    return None


def chunk(
    payload: bytes, request_id: str = "req-1", session_id: str = "sess-1"
) -> proxy_pb2.ClientExecutionRequest:
    """A payload message."""
    return proxy_pb2.ClientExecutionRequest(
        request_id=request_id, session_id=session_id, payload=payload
    )


async def stream_of(
    *messages: proxy_pb2.ClientExecutionRequest,
) -> AsyncIterator[proxy_pb2.ClientExecutionRequest]:
    """Turn messages into the async iterator the stub wants."""
    for message in messages:
        yield message


async def execute(
    harness: Harness,
    *messages: proxy_pb2.ClientExecutionRequest,
) -> list[proxy_pb2.ClientExecutionResponse]:
    """Drive one execution and collect the responses."""
    call = harness.proxy.RequestExecution(stream_of(*messages))
    return [response async for response in call]


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_a_request_reaches_the_worker_and_the_result_comes_back(
    harness: Harness,
) -> None:
    await harness.register_worker()

    responses = await execute(harness, header(), chunk(b"pickled-"), chunk(b"payload"))

    assert b"".join(r.payload for r in responses if r.HasField(
        "payload")) == b"result"
    assert len(harness.worker.calls) == 1
    assert harness.worker.calls[0].payload == b"pickled-payload"


async def test_success_is_propagated_to_the_client(harness: Harness) -> None:
    """The previous router never set it, so every response claimed failure."""
    await harness.register_worker()

    responses = await execute(harness, header(), chunk(b"x"))

    assert responses
    assert all(r.success for r in responses)


async def test_the_client_ids_are_carried_through(harness: Harness) -> None:
    await harness.register_worker()

    responses = await execute(harness, header("req-42", "sess-7"), chunk(b"x", "req-42", "sess-7"))

    assert responses
    assert all(r.request_id == "req-42" for r in responses)
    assert all(r.session_id == "sess-7" for r in responses)


async def test_logs_and_payload_are_demultiplexed(harness: Harness) -> None:
    harness.worker.logs = ("first line\n", "second line\n")
    await harness.register_worker()

    responses = await execute(harness, header(), chunk(b"x"))

    assert [r.logs for r in responses if r.HasField("logs")] == [
        "first line\n",
        "second line\n",
    ]
    assert [r.payload for r in responses if r.HasField("payload")] == [
        b"result"]


# ---------------------------------------------------------------------------
# Import hints and resource budgets
# ---------------------------------------------------------------------------


async def test_the_import_hints_reach_the_worker(harness: Harness) -> None:
    """The previous router consumed them and forwarded nothing.

    The worker's `resources` field - the thing checkpoint selection is meant to
    be built on - was always empty.
    """
    await harness.register_worker()

    await execute(harness, header(imports=("torch", "pandas")), chunk(b"x"))

    assert list(harness.worker.calls[0].header.resources) == [
        "pandas", "torch"]


async def test_the_placement_is_stamped_on_the_first_worker_message(
    harness: Harness,
) -> None:
    await harness.register_worker("w-alpha")

    await execute(harness, header(), chunk(b"x"))

    sent = harness.worker.calls[0].header
    assert sent.worker_id == "w-alpha"
    assert sent.request_id == "req-1"
    assert sent.session_id == "sess-1"
    # No budget on the request, so the router applies its default.
    budget = memory_budget(sent)
    assert budget is not None
    assert budget.alloc == 1024 * 1024 * 1024
    assert sent.container_id == "", "an empty container id asks for a cold start"


async def test_a_client_supplied_memory_budget_reaches_the_worker(harness: Harness) -> None:
    await harness.register_worker()

    await execute(harness, header(memory=256 << 20, max_memory=1 << 30), chunk(b"x"))

    budget = memory_budget(harness.worker.calls[0].header)
    assert budget is not None
    assert budget.alloc == 256 << 20, "the client's budget, not the 1 GiB default"
    assert budget.max == 1 << 30, "the auto-expand ceiling is forwarded too"


async def test_the_client_is_told_which_worker_and_container_ran_the_call(
    harness: Harness,
) -> None:
    """A client cannot pick a worker, but it can report which one was slow."""
    await harness.register_worker("w-alpha")
    harness.worker.container_id = "ctr-7"

    responses = await execute(harness, header(), chunk(b"x"))

    assert responses
    assert all(r.worker_id == "w-alpha" for r in responses)
    assert all(r.container_id == "ctr-7" for r in responses)


# ---------------------------------------------------------------------------
# Protocol violations
# ---------------------------------------------------------------------------


async def test_a_payload_first_message_is_rejected(harness: Harness) -> None:
    """The previous router returned an empty, successful stream instead."""
    await harness.register_worker()

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await execute(harness, chunk(b"straight to the payload"))

    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert not harness.worker.calls


async def test_an_empty_stream_is_rejected(harness: Harness) -> None:
    await harness.register_worker()

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await execute(harness)

    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_a_missing_session_id_is_accepted_as_not_part_of_any_session(
    harness: Harness,
) -> None:
    """Unlike request_id, an empty session_id is valid input, not an error.

    It is what every call without an explicit session sends - see
    identity.py and Client._prepare - so the router must run it normally
    and must not start tracking a session for it.
    """
    await harness.register_worker()

    responses = await execute(harness, header(session_id=""))

    assert responses
    assert harness.app.state.session("") is None


async def test_reusing_an_expired_session_id_is_rejected_as_not_found(
    harness: Harness,
) -> None:
    """Once a session's container is confirmed gone, its id is retired.

    Distinct from an id the router has simply never seen, which must keep
    cold-starting rather than erroring - see the "not part of any session"
    test above.
    """
    await harness.register_worker()

    responses = await execute(harness, header(session_id="sess-1"), chunk(b"body"))
    container_id = next(r.container_id for r in responses if r.container_id)

    await harness.registry.PostExecutorStatus(
        registry_pb2.ExecutorStatus(
            worker_id="worker-1",
            container_id=container_id,
            session_id="sess-1",
            request_id="req-1",
            status=registry_pb2.STATUS_REMOVED,
        )
    )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await execute(harness, header(session_id="sess-1"), chunk(b"body"))

    assert caught.value.code() == grpc.StatusCode.NOT_FOUND


# ---------------------------------------------------------------------------
# Placement failures
# ---------------------------------------------------------------------------


async def test_a_cold_router_reports_unavailable(harness: Harness) -> None:
    """The previous router raised KeyError, which reached the client as UNKNOWN."""
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await execute(harness, header(), chunk(b"x"))

    assert caught.value.code() == grpc.StatusCode.UNAVAILABLE


async def test_a_worker_error_is_mapped_not_leaked(harness: Harness) -> None:
    await harness.register_worker()
    harness.worker.error = grpc.aio.AioRpcError(
        code=grpc.StatusCode.RESOURCE_EXHAUSTED,
        initial_metadata=grpc.aio.Metadata(),
        trailing_metadata=grpc.aio.Metadata(),
        details="could not provision a container",
    )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await execute(harness, header(), chunk(b"x"))

    assert caught.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED


async def test_a_worker_dying_after_partial_output_still_fails_the_client(
    harness: Harness,
) -> None:
    # A worker that streams real output and then breaks (crash, connection
    # drop) must not leave the client with a truncated "success" -- the
    # payload already sent is not the whole story, and success was never
    # confirmed.
    await harness.register_worker()
    harness.worker.reply = b"partial-result"
    harness.worker.error_after_output = True
    harness.worker.error = grpc.aio.AioRpcError(
        code=grpc.StatusCode.UNAVAILABLE,
        initial_metadata=grpc.aio.Metadata(),
        trailing_metadata=grpc.aio.Metadata(),
        details="worker connection lost",
    )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await execute(harness, header(), chunk(b"x"))

    assert caught.value.code() == grpc.StatusCode.UNAVAILABLE


async def test_a_failed_execution_releases_its_reservation(harness: Harness) -> None:
    await harness.register_worker("w1")
    harness.worker.error = grpc.aio.AioRpcError(
        code=grpc.StatusCode.INTERNAL,
        initial_metadata=grpc.aio.Metadata(),
        trailing_metadata=grpc.aio.Metadata(),
        details="boom",
    )

    with pytest.raises(grpc.aio.AioRpcError):
        await execute(harness, header(), chunk(b"x"))

    worker = harness.app.state.worker("w1")
    assert worker is not None
    assert worker.inflight == 0, "a failed execution must not leak its reservation"
    assert worker.reserved_bytes == 0


# ---------------------------------------------------------------------------
# Channel reuse
# ---------------------------------------------------------------------------


async def test_two_requests_share_one_worker_connection(harness: Harness) -> None:
    """The previous router opened a channel per request and said so in a TODO."""
    await harness.register_worker()

    await execute(harness, header("req-1", "sess-1"), chunk(b"a", "req-1", "sess-1"))
    await execute(harness, header("req-2", "sess-2"), chunk(b"b", "req-2", "sess-2"))

    assert len(harness.worker.calls) == 2
    assert {call.target for call in harness.worker.calls} == {"worker-1:50052"}


# ---------------------------------------------------------------------------
# A silent execution
# ---------------------------------------------------------------------------


async def test_an_execution_that_produces_nothing_still_completes(
    harness: Harness, worker: FakeWorker
) -> None:
    """The executor swallows a user exception and sends nothing.

    The router must finish cleanly rather than hanging; the client is the layer
    that turns "no payload" into an error.
    """
    worker.silent = True
    await harness.register_worker("w1")

    responses = await execute(harness, header(), chunk(b"x"))

    assert responses == []
    state_worker = harness.app.state.worker("w1")
    assert state_worker is not None
    assert state_worker.inflight == 0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_a_client_hanging_up_cancels_the_worker_call(harness: Harness) -> None:
    harness.worker.response_delay = 30.0
    await harness.register_worker("w1")

    call = harness.proxy.RequestExecution(stream_of(header(), chunk(b"x")))
    await asyncio.sleep(0.2)
    call.cancel()

    await asyncio.sleep(0.3)
    assert harness.worker.calls
    assert harness.worker.calls[0].cancelled, "the worker call must not outlive the client"

    state_worker = harness.app.state.worker("w1")
    assert state_worker is not None
    assert state_worker.inflight == 0, "cancellation must still release the reservation"
