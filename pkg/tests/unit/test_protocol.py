"""The wire protocol, with no server anywhere."""

from __future__ import annotations

import pytest

from readie._proto import proxy_pb2
from readie.budget import Budget, ResourceKind
from readie.errors import EmptyResultError, RemoteExecutionError
from readie.protocol import (
    Attribution,
    CallRef,
    Outcome,
    RequestEncoder,
    ResponseAssembler,
    to_config,
    unwrap_envelope,
)

REF = CallRef(request_id="req-1", session_id="sess-1")


def encode(
    payload: bytes,
    *,
    chunk_size: int = 4,
    imports: tuple[str, ...] = (),
    budgets: tuple[Budget, ...] = (),
) -> list[proxy_pb2.ClientExecutionRequest]:
    encoder = RequestEncoder(chunk_size=chunk_size)
    return list(encoder.encode(REF, payload, imports, budgets))


def test_the_header_comes_first_and_carries_the_config() -> None:
    budget = Budget(ResourceKind.MEMORY, alloc=1 << 20, max=1 << 21)
    messages = encode(b"abcd", imports=("torch",), budgets=(budget,))

    assert messages[0].WhichOneof("data") == "config"
    assert list(messages[0].config.imports) == ["torch"]
    assert messages[0].config.budgets[0].kind == int(ResourceKind.MEMORY)
    assert messages[0].config.budgets[0].alloc == 1 << 20
    assert messages[0].config.budgets[0].max == 1 << 21


def test_exactly_one_header_is_sent() -> None:
    # The header is the config arm of the oneof; payload chunks are the other.
    # Exactly one config message must lead the stream.
    messages = encode(b"a" * 100, chunk_size=4)
    headers = [m for m in messages if m.WhichOneof("data") == "config"]
    assert len(headers) == 1


def test_every_message_carries_both_identifiers() -> None:
    # The router leases on request_id and binds affinity on session_id, and it
    # reads them off whichever message it happens to be holding.
    for message in encode(b"abcdefghij"):
        assert message.request_id == "req-1"
        assert message.session_id == "sess-1"


def test_the_payload_chunks_and_reassembles_exactly() -> None:
    payload = bytes(range(256)) * 40
    messages = encode(payload, chunk_size=97)
    chunks = [m.payload for m in messages if m.WhichOneof("data") == "payload"]

    assert b"".join(chunks) == payload
    assert all(len(c) <= 97 for c in chunks)
    assert len(chunks) > 1


def test_an_empty_payload_still_sends_a_header() -> None:
    messages = encode(b"")
    assert len(messages) == 1
    assert messages[0].WhichOneof("data") == "config"


def test_a_nonpositive_chunk_size_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        RequestEncoder(chunk_size=0)


def test_the_config_is_built_from_imports_and_budgets() -> None:
    message = to_config(
        ("a", "b"),
        (Budget(ResourceKind.GPU_MEMORY, alloc=4 << 30, max=8 << 30),),
    )

    assert isinstance(message, proxy_pb2.ExecutionConfig)
    assert list(message.imports) == ["a", "b"]
    assert message.budgets[0].kind == int(ResourceKind.GPU_MEMORY)
    assert message.budgets[0].max == 8 << 30


def test_the_config_carries_the_gpu_flag() -> None:
    assert to_config(("a",), (), gpu=True).gpu is True
    assert to_config(("a",), ()).gpu is False


def test_the_config_carries_the_disable_optimized_execution_flag() -> None:
    assert (
        to_config(("a",), (), disable_optimized_execution=True).disable_optimized_execution is True
    )
    assert to_config(("a",), ()).disable_optimized_execution is False


# ---------------------------------------------------------------------------
# Assembling
# ---------------------------------------------------------------------------
def response(
    *,
    success: bool = True,
    payload: bytes | None = None,
    logs: str | None = None,
    worker_id: str = "",
    container_id: str = "",
) -> proxy_pb2.ClientExecutionResponse:
    message = proxy_pb2.ClientExecutionResponse(
        request_id="req-1", success=success, worker_id=worker_id, container_id=container_id
    )
    if payload is not None:
        message.payload = payload
    elif logs is not None:
        message.logs = logs
    return message


def test_payload_messages_concatenate_in_order() -> None:
    assembler = ResponseAssembler()
    for piece in (b"abc", b"def", b"ghi"):
        assembler.accept(response(payload=piece))

    assert assembler.result() == b"abcdefghi"


def test_logs_are_collected_and_streamed_as_they_arrive() -> None:
    seen: list[str] = []
    assembler = ResponseAssembler(on_log=seen.append)
    assembler.accept(response(logs="line one"))
    assembler.accept(response(payload=b"x"))
    assembler.accept(response(logs="line two"))

    assert seen == ["line one", "line two"]
    assert assembler.logs == ("line one", "line two")
    assert assembler.result() == b"x"


def test_no_payload_raises_with_the_captured_output() -> None:
    # The executor's user-exception path: traceback to stderr, nothing on the
    # socket, worker reports success. The old client unpickled the empty buffer
    # and raised a bare EOFError, discarding the traceback.
    assembler = ResponseAssembler()
    assembler.accept(response(logs="Traceback (most recent call last):"))
    assembler.accept(response(logs="ZeroDivisionError: division by zero"))

    with pytest.raises(EmptyResultError) as caught:
        assembler.result()

    assert "ZeroDivisionError" in str(caught.value)
    assert caught.value.logs[-1].endswith("division by zero")


def test_success_false_is_honoured_and_sticky() -> None:
    assembler = ResponseAssembler()
    assembler.accept(response(success=False, logs="bad"))
    assembler.accept(response(success=True, payload=b"partial"))

    with pytest.raises(RemoteExecutionError):
        assembler.result()


def test_a_silent_stream_is_an_empty_result_not_an_empty_success() -> None:
    assembler = ResponseAssembler()
    assert assembler.message_count == 0
    with pytest.raises(EmptyResultError):
        assembler.result()


def test_attribution_takes_the_first_nonempty_identifiers() -> None:
    assembler = ResponseAssembler()
    assembler.accept(response(logs="starting", worker_id="w-1", container_id="ctr-1"))
    assembler.accept(response(payload=b"x", worker_id="w-2", container_id="ctr-2"))

    assert assembler.attribution.worker_id == "w-1"
    assert assembler.attribution.container_id == "ctr-1"


def test_a_failure_carries_where_it_ran() -> None:
    assembler = ResponseAssembler()
    assembler.accept(response(logs="boom", worker_id="w-3", container_id="ctr-3"))

    with pytest.raises(EmptyResultError) as caught:
        assembler.result()

    assert caught.value.worker_id == "w-3"
    assert caught.value.container_id == "ctr-3"


# ---------------------------------------------------------------------------
# The result envelope
#
# Under executor protocol 1 a remote exception was reported by sending nothing,
# so the client inferred failure from an empty payload and scraped the traceback
# out of captured stderr. Protocol 2 sends an envelope either way.
# ---------------------------------------------------------------------------
def outcome(worker: str = "w-1", container: str = "ctr-1") -> Outcome:
    return Outcome(
        payload=b"",
        logs=("a log line",),
        attribution=Attribution(worker_id=worker, container_id=container),
    )


def test_a_success_envelope_unwraps_to_its_value() -> None:
    assert unwrap_envelope({"ok": True, "value": 42}, outcome()) == 42


def test_none_is_a_value_not_an_absence() -> None:
    # The whole reason the envelope exists: "returned None" and "sent nothing"
    # were indistinguishable on the wire under protocol 1.
    assert unwrap_envelope({"ok": True, "value": None}, outcome()) is None


def test_a_failure_envelope_raises_with_the_remote_traceback() -> None:
    envelope = {
        "ok": False,
        "exc_type": "ZeroDivisionError",
        "message": "division by zero",
        "traceback": 'Traceback (most recent call last):\n  File "<x>", line 2\nZeroDivisionError',
    }

    with pytest.raises(RemoteExecutionError) as caught:
        unwrap_envelope(envelope, outcome())

    error = caught.value
    assert error.remote_type == "ZeroDivisionError"
    assert error.remote_message == "division by zero"
    assert "ZeroDivisionError" in error.remote_traceback


def test_the_remote_traceback_appears_in_the_rendered_message() -> None:
    # It has to show up in an unhandled-exception dump, not only on an attribute
    # nobody thinks to look at. The local frames explain nothing.
    with pytest.raises(RemoteExecutionError) as caught:
        unwrap_envelope(
            {"ok": False, "exc_type": "ValueError", "message": "bad", "traceback": "TB-MARKER"},
            outcome(),
        )

    assert "TB-MARKER" in str(caught.value)
    assert "remote traceback" in str(caught.value)


def test_a_failure_names_where_it_ran() -> None:
    with pytest.raises(RemoteExecutionError) as caught:
        unwrap_envelope(
            {"ok": False, "exc_type": "E", "message": "m", "traceback": "t"},
            outcome(worker="w-alpha", container="ctr-7"),
        )

    assert caught.value.worker_id == "w-alpha"
    assert caught.value.container_id == "ctr-7"


def test_a_failure_carries_the_captured_logs_too() -> None:
    with pytest.raises(RemoteExecutionError) as caught:
        unwrap_envelope({"ok": False, "exc_type": "E", "message": "m", "traceback": "t"}, outcome())

    assert caught.value.logs == ("a log line",)


@pytest.mark.parametrize("payload", [b"raw bytes", 42, None, [1, 2], {"value": 1}])
def test_something_that_is_not_an_envelope_says_the_peer_may_be_older(payload) -> None:
    # A bare pickled value is exactly what a protocol-1 executor would send.
    with pytest.raises(EmptyResultError, match="older protocol"):
        unwrap_envelope(payload, outcome())


def test_a_failure_envelope_missing_its_fields_still_raises_usefully() -> None:
    with pytest.raises(RemoteExecutionError, match="Exception"):
        unwrap_envelope({"ok": False}, outcome())
