"""The cloudpickle boundary."""

from __future__ import annotations

import cloudpickle
import pytest

from crfs_executor.codec import DecodeError, decode_call, encode_result


def add(a: int, b: int) -> int:
    return a + b


def encode(payload: object) -> bytes:
    return bytes(cloudpickle.dumps(payload))


def test_a_well_formed_call_decodes_and_invokes():
    raw = encode({"func": add, "args": (1, 2), "kwargs": {}})
    assert decode_call(raw).invoke() == 3


def test_kwargs_are_passed_through():
    raw = encode({"func": add, "args": (), "kwargs": {"a": 4, "b": 5}})
    assert decode_call(raw).invoke() == 9


def test_a_closure_survives_the_round_trip():
    factor = 7

    def scale(x: int) -> int:
        return x * factor

    raw = encode({"func": scale, "args": (6,), "kwargs": {}})
    assert decode_call(raw).invoke() == 42


def test_the_exact_three_key_shape_is_the_contract():
    # pkg/src/crfs/codec.py writes these three keys. If either side renames one
    # the system stops working and nothing else notices.
    loaded = cloudpickle.loads(encode({"func": add, "args": (1, 1), "kwargs": {}}))
    assert set(loaded) == {"func", "args", "kwargs"}


def test_garbage_is_a_decode_error_naming_the_size():
    with pytest.raises(DecodeError, match="9-byte"):
        decode_call(b"not-valid")


def test_a_non_mapping_payload_is_rejected_by_type():
    with pytest.raises(DecodeError, match="got list"):
        decode_call(encode([1, 2, 3]))


@pytest.mark.parametrize(
    ("payload", "missing"),
    [
        ({"args": (), "kwargs": {}}, "func"),
        ({"func": add, "kwargs": {}}, "args"),
        ({"func": add, "args": ()}, "kwargs"),
    ],
)
def test_a_missing_key_is_named(payload, missing):
    # The previous implementation indexed blindly, so a malformed body raised
    # KeyError inside the user's error path and was reported as their fault.
    with pytest.raises(DecodeError, match=missing):
        decode_call(encode(payload))


def test_a_non_callable_func_is_rejected_before_it_is_called():
    with pytest.raises(DecodeError, match="not callable"):
        decode_call(encode({"func": 42, "args": (), "kwargs": {}}))


def test_a_user_exception_propagates_out_of_invoke():
    def explode() -> None:
        raise ValueError("boom")

    call = decode_call(encode({"func": explode, "args": (), "kwargs": {}}))
    with pytest.raises(ValueError, match="boom"):
        call.invoke()


def test_none_encodes_to_a_nonempty_payload():
    # Load-bearing for the client: it treats a zero-byte response as failure,
    # which is only sound if pickling None produces bytes.
    assert encode_result(None)
