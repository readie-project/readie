"""The client, against in-memory transports."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from readie.budget import Budget, ResourceKind
from readie.client import Client, Session
from readie.codec import CloudpickleCodec
from readie.config import Settings
from readie.errors import (
    BlockingCallInEventLoopError,
    ClientClosedError,
    ClusterUnavailableError,
)
from tests.fakes.transport import AsyncRecordingTransport, RecordingTransport

SETTINGS = Settings(router_uri="127.0.0.1:1")


def add(a: int, b: int) -> int:
    return a + b


def build(
    result: object = None,
    **kwargs: Any,
) -> tuple[Client, RecordingTransport, AsyncRecordingTransport]:
    sync = RecordingTransport(result)
    a_sync = AsyncRecordingTransport(result)
    client = Client(
        SETTINGS,
        transport=sync,
        async_transport=a_sync,
        **kwargs,
    )
    return client, sync, a_sync


def test_a_call_returns_the_decoded_result() -> None:
    client, _, _ = build(result=42)
    assert client.call(add, (1, 2)) == 42


async def test_an_async_call_returns_the_decoded_result() -> None:
    client, _, _ = build(result="ok")
    assert await client.acall(add, (1, 2)) == "ok"


def test_each_call_gets_a_fresh_request_id() -> None:
    client, sync, _ = build()
    client.call(add)
    client.call(add)

    first, second = (call[0] for call in sync.calls)
    assert first.request_id != second.request_id


def test_calls_without_a_session_get_different_session_ids() -> None:
    # This is what keeps unrelated calls off one container's serialised queue.
    client, sync, _ = build()
    client.call(add)
    client.call(add)

    assert sync.calls[0][0].session_id != sync.calls[1][0].session_id


def test_calls_within_a_session_share_its_id() -> None:
    client, sync, _ = build()
    with client.session() as session:
        client.call(add, session=session)
        client.call(add, session=session)

    assert {call[0].session_id for call in sync.calls} == {session.id}


def test_the_payload_is_what_the_executor_expects() -> None:
    import cloudpickle

    client, sync, _ = build()
    client.call(add, (2, 3))

    loaded = cloudpickle.loads(sync.last[1])
    assert loaded["func"](*loaded["args"], **loaded["kwargs"]) == 5


def test_kwargs_reach_the_payload() -> None:
    import cloudpickle

    client, sync, _ = build()
    client.call(add, (), {"a": 4, "b": 5})

    loaded = cloudpickle.loads(sync.last[1])
    assert loaded["kwargs"] == {"a": 4, "b": 5}
    assert loaded["func"](**loaded["kwargs"]) == 9


def test_a_per_call_timeout_overrides_the_setting() -> None:
    client = Client(
        Settings(router_uri="h:1", timeout=30.0),
        transport=(sync := RecordingTransport()),
        async_transport=AsyncRecordingTransport(),
    )
    client.call(add)
    assert sync.last[4] == 30.0

    client.call(add, timeout=5.0)
    assert sync.last[4] == 5.0


def test_transport_errors_propagate_unchanged() -> None:
    client, sync, _ = build()
    sync.error = ClusterUnavailableError("nope")

    with pytest.raises(ClusterUnavailableError):
        client.call(add)


def test_logs_reach_the_sink_when_streaming_is_on() -> None:
    seen: list[str] = []
    sync = RecordingTransport(1, logs=("a", "b"))
    client = Client(
        SETTINGS,
        transport=sync,
        async_transport=AsyncRecordingTransport(1),
        log_sink=seen.append,
    )
    client.call(add)

    assert seen == ["a", "b"]


def test_function_output_in_the_result_reaches_the_sink() -> None:
    seen: list[str] = []
    client, sync, _ = build(log_sink=seen.append)
    sync.envelope = {"ok": True, "value": 1, "output": ["Inside add function", "\n"]}

    assert client.call(add) == 1
    assert seen == ["Inside add function", "\n"]


def test_streaming_can_be_turned_off() -> None:
    seen: list[str] = []
    sync = RecordingTransport(1, logs=("a",))
    client = Client(
        Settings(router_uri="h:1", stream_logs=False),
        transport=sync,
        async_transport=AsyncRecordingTransport(1),
        log_sink=seen.append,
    )
    client.call(add)

    assert seen == []


async def test_a_blocking_call_from_a_running_loop_is_refused_not_deadlocked() -> None:
    client, _, _ = build()
    with pytest.raises(BlockingCallInEventLoopError, match=r"\.aio"):
        client.call(add)


def test_a_blocking_call_outside_a_loop_is_fine() -> None:
    client, _, _ = build(result=1)
    assert client.call(add) == 1
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()  # and it did not start one


def test_a_closed_client_refuses_work() -> None:
    client, sync, _ = build()
    client.close()

    assert sync.closed
    with pytest.raises(ClientClosedError, match="client is closed"):
        client.call(add)


def test_closing_twice_is_fine() -> None:
    client, _, _ = build()
    client.close()
    client.close()


async def test_aclose_closes_both_transports() -> None:
    client, sync, a_sync = build()
    await client.aclose()
    assert sync.closed
    assert a_sync.closed


def test_a_closed_session_refuses_work_but_the_client_stays_usable() -> None:
    client, _, _ = build(result=1)
    session = Session()
    session.close()

    with pytest.raises(ClientClosedError, match="is closed"):
        client.call(add, session=session)
    assert client.call(add) == 1


def test_the_session_context_manager_closes_on_exit() -> None:
    client, _, _ = build()
    with client.session() as session:
        assert not session.closed
    assert session.closed


def test_the_session_context_manager_closes_on_error() -> None:
    client, _, _ = build()
    opened: list[Session] = []

    def fail_inside() -> None:
        with client.session() as session:
            opened.append(session)
            raise RuntimeError

    with pytest.raises(RuntimeError):
        fail_inside()

    assert opened[0].closed


def test_an_explicit_session_id_is_honoured() -> None:
    client, sync, _ = build()
    with client.session("sess-mine") as session:
        client.call(add, session=session)
    assert sync.last[0].session_id == "sess-mine"


def test_the_client_works_as_a_context_manager() -> None:
    with build(result=3)[0] as client:
        assert client.call(add) == 3
    assert client._closed


async def test_the_client_works_as_an_async_context_manager() -> None:
    client, _, a_sync = build(result=3)
    async with client:
        assert await client.acall(add) == 3
    assert a_sync.closed


def test_a_custom_codec_is_used_for_both_directions() -> None:
    # The codec sees the raw envelope, not the value inside it: substituting a
    # safer serialisation has to be able to reject the whole payload.
    class ShoutingCodec(CloudpickleCodec):
        def decode_result(self, raw):
            envelope = super().decode_result(raw)
            return {**envelope, "value": str(envelope["value"]).upper()}

    client = Client(
        SETTINGS,
        transport=RecordingTransport("quiet"),
        async_transport=AsyncRecordingTransport("quiet"),
        codec=ShoutingCodec(),
    )
    assert client.call(add) == "QUIET"


def test_imports_and_budgets_reach_the_transport() -> None:
    def uses_json():
        import json

        return json

    sync = RecordingTransport(1)
    client = Client(SETTINGS, transport=sync, async_transport=AsyncRecordingTransport(1))
    budgets = (Budget(ResourceKind.MEMORY, alloc=1 << 20),)
    client.call(uses_json, budgets=budgets)

    # Recorded tuple: (ref, payload, imports, budgets, timeout).
    assert "json" in sync.last[2]
    assert sync.last[3] == budgets


def test_repr_names_the_router() -> None:
    client, _, _ = build()
    assert "127.0.0.1:1" in repr(client)
    assert "open" in repr(Session("s"))
