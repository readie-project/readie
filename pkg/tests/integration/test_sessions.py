"""Session identity and concurrency, over real gRPC."""

from __future__ import annotations

import asyncio

from readie.client import Client
from readie.transport import AsyncGrpcTransport
from tests.fakes.router import RouterHarness


def add(a: int, b: int) -> int:
    return a + b


def slow(a: int) -> int:
    return a


async def test_calls_in_one_session_all_carry_its_id(
    client: Client,
    harness: RouterHarness,
) -> None:
    with client.session() as session:
        await client.acall(add, (1, 1), session=session)
        await client.acall(add, (2, 2), session=session)

    assert set(harness.router.session_ids) == {session.id}


async def test_calls_without_a_session_send_no_session_id(
    client: Client,
    harness: RouterHarness,
) -> None:
    await asyncio.gather(*(client.acall(add, (i, i)) for i in range(8)))

    # Independent calls send no session_id at all - the router treats an
    # empty id as "not part of any session" and never tracks or serialises on
    # it, rather than each call getting its own throwaway session just to
    # avoid contention.
    assert set(harness.router.session_ids) == {""}


async def test_request_ids_are_unique_across_concurrent_calls(
    client: Client,
    harness: RouterHarness,
) -> None:
    await asyncio.gather(*(client.acall(add, (i, i)) for i in range(8)))
    assert len(set(harness.router.request_ids)) == 8


async def test_independent_calls_actually_overlap(client: Client, harness: RouterHarness) -> None:
    harness.router.delay = 0.2
    results = await asyncio.gather(*(client.acall(add, (i, 1)) for i in range(4)))

    assert results == [1, 2, 3, 4]
    # The fake router does not serialise, so overlap here proves the *client*
    # does not either -- one channel carries concurrent streams.
    assert harness.router.max_concurrent > 1


async def test_the_channel_is_reused_across_calls(client: Client) -> None:
    # The old client opened a channel per call inside `async with`. One channel
    # object across two calls is the observable form of the fix.
    transport = client._async_transport
    assert isinstance(transport, AsyncGrpcTransport)

    await client.acall(add, (1, 1))
    first = transport._channels.get()
    await client.acall(add, (1, 1))
    second = transport._channels.get()

    assert first is second
