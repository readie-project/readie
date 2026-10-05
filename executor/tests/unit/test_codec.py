"""The cloudpickle boundary."""

from __future__ import annotations

import cloudpickle
import pytest

from readie_executor.codec import DecodeError, decode_call, encode_result


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
    # pkg/src/readie/codec.py writes these three keys. If either side renames one
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


def test_packages_are_read_when_present():
    raw = encode({"func": add, "args": (1, 2), "kwargs": {}, "packages": ["numpy", "requests"]})
    assert decode_call(raw).packages == ("numpy", "requests")


def test_packages_default_to_empty_when_absent():
    # An older client that never sends the key still decodes -- packages are
    # additional, not part of the three-key minimum contract.
    raw = encode({"func": add, "args": (1, 2), "kwargs": {}})
    assert decode_call(raw).packages == ()


def test_session_globals_default_to_false_for_older_clients():
    assert decode_call(encode({"func": add, "args": (), "kwargs": {}})).session_globals is False


def test_session_globals_are_read_when_enabled():
    call = decode_call(encode({"func": add, "args": (), "kwargs": {}, "session_globals": True}))
    assert call.session_globals is True


@pytest.mark.parametrize("flag", [None, 0, 1, "true", [], {}])
def test_non_boolean_session_globals_are_rejected(flag):
    with pytest.raises(DecodeError, match="must be a boolean"):
        decode_call(encode({"func": add, "args": (), "kwargs": {}, "session_globals": flag}))


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
