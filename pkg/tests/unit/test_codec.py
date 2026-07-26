"""Serialisation, and what happens when it cannot work."""

from __future__ import annotations

import threading

import pytest

from crfs.codec import CloudpickleCodec
from crfs.errors import SerializationError


def add(a: int, b: int) -> int:
    return a + b


def test_the_encoded_call_matches_what_the_executor_unpickles() -> None:
    # The executor's codec.py does exactly this. If these three keys ever change
    # spelling, the executor breaks and nothing else notices.
    import cloudpickle

    raw = CloudpickleCodec().encode_call(add, (1, 2), {})
    loaded = cloudpickle.loads(raw)

    assert set(loaded) == {"func", "args", "kwargs"}
    assert loaded["func"](*loaded["args"], **loaded["kwargs"]) == 3


def test_a_closure_over_local_state_survives_the_round_trip() -> None:
    factor = 7

    def scale(x):
        return x * factor

    import cloudpickle

    loaded = cloudpickle.loads(CloudpickleCodec().encode_call(scale, (6,), {}))
    assert loaded["func"](*loaded["args"]) == 42


def test_an_unpicklable_closure_is_a_serialization_error_naming_the_function() -> None:
    lock = threading.Lock()

    def uses_a_lock():
        with lock:
            return 1

    with pytest.raises(SerializationError, match="uses_a_lock"):
        CloudpickleCodec().encode_call(uses_a_lock, (), {})


def test_decoding_garbage_reports_the_size_rather_than_raising_eoferror() -> None:
    with pytest.raises(SerializationError, match="9 bytes"):
        CloudpickleCodec().decode_result(b"not-valid")


def test_none_round_trips_and_is_not_an_empty_payload() -> None:
    # Load-bearing for EmptyResultError: if pickling None produced zero bytes,
    # "no payload means the function raised" would be wrong.
    codec = CloudpickleCodec()
    raw = codec.encode_call(add, (), {})
    assert raw
    import cloudpickle

    assert cloudpickle.dumps(None)
    assert codec.decode_result(cloudpickle.dumps(None)) is None
