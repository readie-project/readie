"""Session signaling, acknowledgments, and filterable remote diagnostics."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import cloudpickle
import pytest

from readie import IgnoredGlobalsWarning, Session, remote
from readie.client import Client
from readie.codec import CloudpickleCodec
from readie.config import Settings
from readie.errors import ConfigurationError, RemoteExecutionError
from tests.fakes.transport import AsyncRecordingTransport, RecordingTransport

WARNING = {
    "code": "ignored_globals",
    "message": "Client globals were ignored.",
    "names": ["client_value"],
}


def task() -> int:
    return 42


def build(
    envelope: dict[str, Any] | None = None,
    *,
    session_globals: bool = True,
) -> tuple[Client, RecordingTransport, AsyncRecordingTransport]:
    sync, asynchronous = RecordingTransport(42), AsyncRecordingTransport(42)
    sync.envelope = asynchronous.envelope = envelope
    client = Client(
        Settings(router_uri="127.0.0.1:1", stream_logs=False),
        transport=sync,
        async_transport=asynchronous,
        session_globals=session_globals,
    )
    return client, sync, asynchronous


def success(**fields: Any) -> dict[str, Any]:
    return {"ok": True, "value": 42, "session_globals_applied": True, **fields}


def test_only_session_calls_enable_the_payload_flag():
    client, sync, _ = build()
    client.call(task)
    assert "session_globals" not in cloudpickle.loads(sync.last[1])
    client.call(task, session=Session())
    assert cloudpickle.loads(sync.last[1])["session_globals"] is True


async def test_async_session_calls_enable_the_same_flag():
    client, _, asynchronous = build()
    await client.acall(task, session=Session())
    assert cloudpickle.loads(asynchronous.last[1])["session_globals"] is True
    await client.acall(task)
    assert "session_globals" not in cloudpickle.loads(asynchronous.last[1])


def test_opted_out_sessions_keep_their_id_without_enabling_globals_or_requiring_ack():
    client, sync, _ = build({"ok": True, "value": 42}, session_globals=False)
    with client.session() as session:
        assert client.call(task, session=session) == 42
        assert client.call(task, session=session) == 42
    assert {call[0].session_id for call in sync.calls} == {session.id}
    assert all("session_globals" not in cloudpickle.loads(call[1]) for call in sync.calls)


async def test_async_opted_out_sessions_keep_their_id_without_enabling_globals_or_requiring_ack():
    client, _, asynchronous = build({"ok": True, "value": 42}, session_globals=False)
    with client.session() as session:
        assert await client.acall(task, session=session) == 42
    assert asynchronous.last[0].session_id == session.id
    assert "session_globals" not in cloudpickle.loads(asynchronous.last[1])


def test_decorated_session_calls_respect_the_client_opt_out():
    client, sync, _ = build({"ok": True, "value": 42}, session_globals=False)
    session = Session()
    wrapped = remote(client=client)(task).bind(session)
    assert wrapped() == 42
    assert sync.last[0].session_id == session.id
    assert "session_globals" not in cloudpickle.loads(sync.last[1])


def test_opted_out_payload_preserves_client_globals_and_ordinary_callable_types():
    namespace: dict[str, Any] = {"__name__": "__readie_opt_out_test__"}
    exec(  # noqa: S102 - trusted test function forces by-value serialization
        "client_value = 99\ndef task():\n    return client_value\n", namespace
    )
    client, sync, _ = build({"ok": True, "value": 42}, session_globals=False)
    session = Session()
    client.call(namespace["task"], session=session)
    loaded = cloudpickle.loads(sync.last[1])
    assert loaded["func"]() == 99
    client.call(len, ([1, 2],), session=session)
    loaded = cloudpickle.loads(sync.last[1])
    assert loaded["func"](*loaded["args"], **loaded["kwargs"]) == 2
    assert sync.last[0].session_id == session.id
    assert "session_globals" not in loaded


def test_clients_can_choose_different_globals_modes_for_the_same_session():
    enabled, enabled_transport, _ = build()
    disabled, disabled_transport, _ = build({"ok": True, "value": 42}, session_globals=False)
    session = Session()
    enabled.call(task, session=session)
    disabled.call(task, session=session)
    assert (
        enabled_transport.last[0].session_id == disabled_transport.last[0].session_id == session.id
    )
    assert cloudpickle.loads(enabled_transport.last[1])["session_globals"] is True
    assert "session_globals" not in cloudpickle.loads(disabled_transport.last[1])


def test_warning_is_delivered_even_with_logs_disabled_and_points_to_the_caller():
    client, _, _ = build(success(warnings=[WARNING]))
    with pytest.warns(IgnoredGlobalsWarning) as captured:
        assert client.call(task, session=Session()) == 42
    assert captured[0].filename == str(Path(__file__))


async def test_async_warning_delivery_uses_the_common_finish_path():
    client, _, _ = build(success(warnings=[WARNING]))
    with pytest.warns(IgnoredGlobalsWarning) as captured:
        assert await client.acall(task, session=Session()) == 42
    assert captured[0].filename == str(Path(__file__))


def test_decorated_warning_points_outside_the_sdk():
    client, _, _ = build(success(warnings=[WARNING]))
    wrapped = remote(client=client, session=Session())(task)
    with pytest.warns(IgnoredGlobalsWarning) as captured:
        assert wrapped() == 42
    assert captured[0].filename == str(Path(__file__))


def test_warnings_can_be_suppressed_or_promoted_after_execution():
    client, sync, _ = build(success(warnings=[WARNING]))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", IgnoredGlobalsWarning)
        assert client.call(task, session=Session()) == 42
    with warnings.catch_warnings():
        warnings.simplefilter("error", IgnoredGlobalsWarning)
        with pytest.raises(IgnoredGlobalsWarning):
            client.call(task, session=Session())
    assert len(sync.calls) == 2


@pytest.mark.parametrize("acknowledgment", [None, False, 1, "true"])
def test_old_or_invalid_acknowledgments_refuse_a_success_without_retry(acknowledgment):
    client, sync, _ = build({"ok": True, "value": 42, "session_globals_applied": acknowledgment})
    with pytest.raises(ConfigurationError, match="may already have executed"):
        client.call(task, session=Session())
    assert len(sync.calls) == 1


async def test_async_old_executor_results_are_rejected_too():
    client, _, asynchronous = build({"ok": True, "value": 42})
    with pytest.raises(ConfigurationError, match="did not acknowledge"):
        await client.acall(task, session=Session())
    assert len(asynchronous.calls) == 1


def test_unscoped_old_executor_results_still_work():
    client, _, _ = build({"ok": True, "value": 42})
    assert client.call(task) == 42


def test_failed_calls_warn_before_raising_and_do_not_require_an_acknowledgment():
    client, _, _ = build(
        {
            "ok": False,
            "exc_type": "NameError",
            "message": "client_value is not defined",
            "traceback": "remote traceback",
            "warnings": [WARNING],
        }
    )
    with pytest.warns(IgnoredGlobalsWarning), pytest.raises(RemoteExecutionError) as raised:
        client.call(task, session=Session())
    assert raised.value.remote_type == "NameError"


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        "warning",
        [None],
        [{"code": "future_warning", "message": "unknown", "names": []}],
        [{"code": "ignored_globals", "message": 42, "names": []}],
        [{"code": "ignored_globals", "message": "bad", "names": [1]}],
        [{"code": "ignored_globals", "message": "bad"}],
    ],
)
def test_unknown_or_malformed_warning_metadata_does_not_break_results(metadata):
    client, _, _ = build(success(warnings=metadata))
    assert client.call(task, session=Session()) == 42


def test_custom_codecs_receive_the_session_keyword():
    class RecordingCodec(CloudpickleCodec):
        def __init__(self) -> None:
            self.flags: list[bool] = []

        def encode_call(self, func, args, kwargs, packages=(), *, session_globals=False):
            self.flags.append(session_globals)
            return super().encode_call(
                func, args, kwargs, packages, session_globals=session_globals
            )

    client, _, _ = build()
    codec = RecordingCodec()
    client._codec = codec
    client.call(task)
    client.call(task, session=Session())
    assert codec.flags == [False, True]


@pytest.mark.parametrize(
    ("session_globals", "use_session"), [(True, False), (False, False), (False, True)]
)
def test_legacy_custom_codecs_work_for_unscoped_and_opted_out_calls(session_globals, use_session):
    class LegacyCodec:
        def encode_call(self, func, args, kwargs, packages=()):
            return CloudpickleCodec().encode_call(func, args, kwargs, packages)

        def decode_result(self, raw):
            return cloudpickle.loads(raw)

    client, _, _ = build(session_globals=session_globals)
    # This intentionally exercises an old runtime implementation of the seam.
    client._codec = LegacyCodec()  # type: ignore[assignment]
    assert client.call(task, session=Session() if use_session else None) == 42


def test_the_public_warning_is_filterable_without_being_a_readie_error():
    assert issubclass(IgnoredGlobalsWarning, UserWarning)
