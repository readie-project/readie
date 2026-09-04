"""The client against a real ProxyService over real gRPC."""

from __future__ import annotations

import asyncio

import cloudpickle
import grpc
import pytest

import readie
from readie.client import Client
from readie.config import Settings
from tests.fakes.router import RouterHarness


def add(a: int, b: int) -> int:
    return a + b


async def test_a_call_round_trips_through_grpc(client: Client) -> None:
    assert await client.acall(add, (2, 40)) == 42


async def test_the_router_sees_a_header_then_payload_chunks(
    client: Client,
    harness: RouterHarness,
) -> None:
    await client.acall(add, (1, 1))

    assert len(harness.router.headers) == 1
    assert harness.router.payload_messages
    assert all(m.session_id for m in harness.router.payload_messages)


async def test_a_large_payload_survives_chunking_over_the_wire(harness: RouterHarness) -> None:
    # Two full chunks plus a remainder, so the framing is genuinely exercised.
    client = Client(Settings(router_uri=harness.uri, tls=False, chunk_size=64 * 1024, stream_logs=False))
    blob = bytes(range(256)) * 1024  # 256 KiB

    def echo(payload):
        return payload

    try:
        assert await client.acall(echo, (blob,)) == blob
    finally:
        await client.aclose()

    assert len(harness.router.payload_messages) > 2


async def test_executor_logs_stream_to_the_sink_before_the_result(harness: RouterHarness) -> None:
    seen: list[str] = []
    harness.router.logs = ["starting", "done"]
    client = Client(Settings(router_uri=harness.uri, tls=False), log_sink=seen.append)

    try:
        assert await client.acall(add, (1, 2)) == 3
    finally:
        await client.aclose()

    assert seen == ["starting", "done"]


async def test_a_remote_exception_arrives_with_its_real_traceback(
    harness: RouterHarness,
) -> None:
    # The behaviour executor protocol 2 exists for. Under protocol 1 this call
    # returned nothing at all and the client raised EmptyResultError, leaving
    # the traceback to be scraped out of captured stderr.
    def explode() -> None:
        raise ValueError("bad input")

    client = Client(Settings(router_uri=harness.uri, tls=False, stream_logs=False))
    try:
        with pytest.raises(readie.RemoteExecutionError) as caught:
            await client.acall(explode)
    finally:
        await client.aclose()

    error = caught.value
    assert error.remote_type == "ValueError"
    assert error.remote_message == "bad input"
    assert "ValueError: bad input" in error.remote_traceback
    assert "explode" in error.remote_traceback, "the frame that raised is named"


async def test_a_worker_that_sends_nothing_at_all_is_an_empty_result(
    harness: RouterHarness,
) -> None:
    # Now a protocol violation rather than an everyday outcome: the executor
    # died before it could report anything.
    harness.router.swallow_result = True
    harness.router.logs = ["Traceback (most recent call last):", "MemoryError"]
    client = Client(Settings(router_uri=harness.uri, tls=False, stream_logs=False))

    try:
        with pytest.raises(readie.EmptyResultError) as caught:
            await client.acall(add, (1, 2))
    finally:
        await client.aclose()

    assert "died before it could report" in str(caught.value)
    assert caught.value.logs[-1] == "MemoryError"


async def test_a_bare_value_instead_of_an_envelope_names_the_likely_cause(
    harness: RouterHarness,
) -> None:
    # A bare pickled value is exactly what a protocol-1 executor would send.
    harness.router.raw_payload = cloudpickle.dumps(42)
    client = Client(Settings(router_uri=harness.uri, tls=False, stream_logs=False))

    try:
        with pytest.raises(readie.EmptyResultError, match="older protocol"):
            await client.acall(add, (1, 2))
    finally:
        await client.aclose()


async def test_a_failure_names_the_worker_and_container_that_ran_it(
    harness: RouterHarness,
) -> None:
    harness.router.swallow_result = True
    harness.router.worker_id = "w-alpha"
    harness.router.container_id = "ctr-7"
    harness.router.logs = ["ValueError: boom"]
    client = Client(Settings(router_uri=harness.uri, tls=False, stream_logs=False))

    try:
        with pytest.raises(readie.EmptyResultError) as caught:
            await client.acall(add, (1, 2))
    finally:
        await client.aclose()

    assert caught.value.worker_id == "w-alpha"
    assert caught.value.container_id == "ctr-7"


async def test_success_false_surfaces_as_a_remote_execution_error(harness: RouterHarness) -> None:
    harness.router.success = False
    client = Client(Settings(router_uri=harness.uri, tls=False, stream_logs=False))

    try:
        with pytest.raises(readie.RemoteExecutionError):
            await client.acall(add, (1, 2))
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (grpc.StatusCode.UNAVAILABLE, readie.ClusterUnavailableError),
        (grpc.StatusCode.INVALID_ARGUMENT, readie.InvalidRequestError),
        (grpc.StatusCode.RESOURCE_EXHAUSTED, readie.ResourceExhaustedError),
        (grpc.StatusCode.PERMISSION_DENIED, readie.PermissionDeniedError),
        (grpc.StatusCode.INTERNAL, readie.TransportError),
    ],
)
async def test_status_codes_map_onto_the_error_hierarchy(
    harness: RouterHarness,
    code: grpc.StatusCode,
    expected: type[readie.TransportError],
) -> None:
    harness.router.abort_with = (code, "refused")
    client = Client(Settings(router_uri=harness.uri, tls=False, stream_logs=False))

    try:
        with pytest.raises(expected) as caught:
            await client.acall(add, (1, 2))
    finally:
        await client.aclose()

    assert caught.value.code == code.name


async def test_an_empty_cluster_reads_as_unavailable_with_a_useful_message(
    harness: RouterHarness,
) -> None:
    harness.router.abort_with = (grpc.StatusCode.UNAVAILABLE, "no worker has capacity")
    client = Client(Settings(router_uri=harness.uri, tls=False, stream_logs=False))

    try:
        with pytest.raises(readie.ClusterUnavailableError, match="no worker with capacity"):
            await client.acall(add, (1, 2))
    finally:
        await client.aclose()


async def test_an_unreachable_router_is_unavailable_not_a_raw_grpc_error() -> None:
    client = Client(Settings(router_uri="127.0.0.1:1", timeout=2.0, stream_logs=False))
    try:
        with pytest.raises(readie.ClusterUnavailableError):
            await client.acall(add, (1, 2))
    finally:
        await client.aclose()


async def test_a_deadline_is_enforced_and_reported(harness: RouterHarness) -> None:
    harness.router.delay = 5.0
    client = Client(Settings(router_uri=harness.uri, tls=False, timeout=0.3, stream_logs=False))

    try:
        with pytest.raises(readie.RemoteTimeoutError):
            await client.acall(add, (1, 2))
    finally:
        await client.aclose()


async def test_cancelling_the_caller_cancels_the_rpc(harness: RouterHarness) -> None:
    harness.router.delay = 5.0
    client = Client(Settings(router_uri=harness.uri, tls=False, stream_logs=False))

    task = asyncio.create_task(client.acall(add, (1, 2)))
    await asyncio.sleep(0.2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The router must observe the cancellation rather than hold a lease open.
    await asyncio.sleep(0.1)
    assert harness.router.concurrent == 0
    await client.aclose()
