"""Framing and the result envelope, with no socket anywhere."""

from __future__ import annotations

import struct

import pytest

from crfs_executor.protocol import (
    LENGTH_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_length,
    encode_length,
    failure_envelope,
    read_exactly,
    read_message,
    success_envelope,
    validate_envelope,
    write_message,
)
from tests.fakes import BrokenWriter, RecordingWriter, ScriptedReader, dribbling

CHUNK = 64


def framed(payload: bytes, *, chunk: int = 1 << 30) -> bytes:
    """Encode a payload the way write_message does."""
    out = b"".join(
        encode_length(len(payload[i : i + chunk])) + payload[i : i + chunk]
        for i in range(0, len(payload), chunk)
    )
    return out + encode_length(0)


def read(chunks: list[bytes], **kwargs: object) -> bytes:
    return read_message(ScriptedReader(chunks), chunk_size=CHUNK, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The length prefix
# ---------------------------------------------------------------------------
def test_the_prefix_is_eight_bytes_big_endian():
    # Pinned rather than derived: worker/internal/executor decodes this with
    # binary.BigEndian.Uint64 and the two must agree exactly.
    assert LENGTH_BYTES == 8
    assert encode_length(1) == b"\x00\x00\x00\x00\x00\x00\x00\x01"
    assert encode_length(258) == b"\x00\x00\x00\x00\x00\x00\x01\x02"


def test_the_prefix_round_trips():
    for n in (0, 1, 255, 256, 65535, 1 << 20, (1 << 32) + 7):
        assert decode_length(encode_length(n)) == n


def test_a_prefix_of_the_wrong_size_is_rejected():
    with pytest.raises(ProtocolError, match="8 bytes"):
        decode_length(b"\x00\x01")


def test_the_version_is_two():
    assert PROTOCOL_VERSION == 2


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def test_a_frame_in_one_recv():
    assert read([framed(b"hello")]) == b"hello"


def test_a_frame_split_across_recvs_reassembles():
    whole = framed(b"hello world")
    assert read([whole[:3], whole[3:9], whole[9:]]) == b"hello world"


def test_a_frame_delivered_one_byte_at_a_time_reassembles():
    # Including the prefix split across eight separate reads, which is the case
    # every hand-rolled length-prefix implementation gets wrong first.
    payload = b"a moderately long body, delivered the hard way"
    assert read_message(dribbling(framed(payload)), chunk_size=CHUNK) == payload


def test_a_body_containing_the_old_terminator_is_no_longer_special():
    # Version 1 could not represent this at all: the bytes were the terminator.
    for body in (b"EOF", b"EOFEOF", b"trailing-EOF", b"OFFEE"):
        assert read([framed(body)]) == body


def test_an_empty_frame_is_legal():
    assert read([framed(b"")]) == b""


def test_a_body_far_larger_than_the_chunk_size():
    payload = bytes(range(256)) * 40  # ~10 KiB against a 64-byte chunk
    assert read([framed(payload)]) == payload


def test_a_stream_that_ends_mid_prefix_is_an_error():
    with pytest.raises(ProtocolError, match="of 8 expected"):
        read([b"\x00\x00\x00"])


def test_a_stream_that_ends_before_the_terminator_is_an_error():
    # Every chunk arrived intact and the message is still incomplete. Version 1
    # had no way to tell this from a finished response.
    with pytest.raises(ProtocolError, match="0 of 8 expected"):
        read([encode_length(4) + b"abcd"])


def test_a_stream_that_ends_mid_body_is_an_error_not_a_short_read():
    # The defect this framing exists to fix. Under version 1 an executor killed
    # mid-write produced a short response that looked exactly like a small one.
    with pytest.raises(ProtocolError, match="4 of 100 expected"):
        read([encode_length(100) + b"abcd"])


def test_an_immediately_closed_stream_is_an_error():
    with pytest.raises(ProtocolError, match="0 of 8"):
        read([])


def test_a_multi_chunk_message_joins_in_order():
    body = b"".join(bytes([i]) * 100 for i in range(10))
    assert read([framed(body, chunk=100)]) == body


def test_an_implausible_chunk_length_is_refused_before_anything_is_allocated():
    # The prefix comes from the peer. Trusting it as an allocation size lets one
    # corrupt chunk OOM-kill the sandbox, which reads as a disappearance.
    huge = struct.pack(">Q", 1 << 62)
    with pytest.raises(ProtocolError, match="over the"):
        read([huge], max_chunk=1024)


def test_a_message_of_many_small_chunks_is_still_bounded():
    # The per-chunk cap alone does not stop a peer sending small chunks forever.
    stream = (encode_length(10) + b"x" * 10) * 20
    with pytest.raises(ProtocolError, match="message exceeded"):
        read([stream], max_bytes=50)


def test_read_exactly_returns_nothing_for_zero():
    assert read_exactly(ScriptedReader([]), 0, chunk_size=CHUNK) == b""


def test_reading_never_over_reads_into_the_next_message():
    # Two messages back to back. Reading the first must leave the second intact,
    # or a reused connection desynchronises on its second request.
    reader = ScriptedReader([framed(b"first") + framed(b"second")])

    assert read_message(reader, chunk_size=CHUNK) == b"first"
    assert read_message(reader, chunk_size=CHUNK) == b"second"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def test_a_message_round_trips():
    writer = RecordingWriter()
    write_message(writer, b"result bytes", chunk_size=CHUNK)

    assert writer.data == framed(b"result bytes")
    assert read([writer.data]) == b"result bytes"


def test_each_prefix_travels_with_its_own_chunk_not_alone():
    # A lone 8-byte write is a separate packet on some transports; there is no
    # reason to wake the reader twice per chunk.
    writer = RecordingWriter()
    write_message(writer, b"x" * 200, chunk_size=CHUNK)

    assert len(writer.writes[0]) == LENGTH_BYTES + CHUNK
    assert writer.writes[-1] == encode_length(0), "the terminator is its own write"


def test_a_large_message_is_chunked_and_reassembles():
    writer = RecordingWriter()
    payload = bytes(range(256)) * 64  # 16 KiB
    write_message(writer, payload, chunk_size=CHUNK)

    assert read([writer.data]) == payload
    assert all(len(w) <= LENGTH_BYTES + CHUNK for w in writer.writes)


def test_an_empty_message_is_just_the_terminator():
    writer = RecordingWriter()
    write_message(writer, b"", chunk_size=CHUNK)

    assert writer.data == encode_length(0)
    assert read([writer.data]) == b""


def test_a_peer_hanging_up_mid_message_surfaces_as_oserror():
    with pytest.raises(BrokenPipeError):
        write_message(BrokenWriter(fail_after=1), b"x" * (CHUNK * 3), chunk_size=CHUNK)


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------
def test_a_success_envelope_carries_the_value():
    envelope = success_envelope(42)
    assert envelope == {"ok": True, "value": 42}
    assert validate_envelope(envelope)["value"] == 42


def test_a_success_envelope_can_carry_none():
    # Distinguishing "returned None" from "sent nothing" is the point of having
    # an envelope at all.
    assert validate_envelope(success_envelope(None))["value"] is None


def test_a_failure_envelope_carries_type_message_and_traceback():
    try:
        raise ValueError("boom")
    except ValueError as exc:
        envelope = failure_envelope(exc, "Traceback...\nValueError: boom")

    assert envelope["ok"] is False
    assert envelope["exc_type"] == "ValueError"
    assert envelope["message"] == "boom"
    assert "ValueError: boom" in envelope["traceback"]


def test_a_failure_envelope_holds_strings_not_the_exception_object():
    # Sending the object would need its class importable on the client, which
    # for a library that only exists in the worker image it is not -- the
    # resulting ImportError would replace the real error with a confusing one.
    class WorkerOnlyError(Exception):
        pass

    envelope = failure_envelope(WorkerOnlyError("nope"), "tb")
    assert all(isinstance(v, str) for k, v in envelope.items() if k != "ok")


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (b"not a dict", "got bytes"),
        ({"value": 1}, "protocol 1"),
        ({"ok": True}, "no 'value'"),
        ({"ok": False, "message": "x"}, "exc_type"),
        ({"ok": False}, "exc_type, message, traceback"),
    ],
)
def test_a_malformed_envelope_is_rejected_with_an_explanation(payload, match):
    with pytest.raises(ProtocolError, match=match):
        validate_envelope(payload)
