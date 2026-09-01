"""The unix-socket wire protocol, as pure functions over a byte stream.

Nothing here opens a socket. Everything operates on the two-method ``Reader``
and ``Writer`` seams below, so framing is testable with an in-memory stream and
with one that hands over a single byte at a time.

# Protocol (version 2)

A message is a sequence of length-prefixed chunks ending in a zero-length one::

    message:   ([8-byte big-endian length][chunk])* [8 zero bytes]

    request:   one message, the cloudpickle of {func, args, kwargs}
    response:  one message, the cloudpickle of an envelope

    envelope = {"ok": True,  "value": <return value>}
             | {"ok": False, "exc_type": str, "message": str, "traceback": str}

Framed per chunk rather than once per message so that a sender can stream
without knowing the total size in advance. The worker relays request bytes from
a gRPC stream as they arrive and learns the length only when that stream ends;
a single prefix would force it to buffer an entire payload — possibly hundreds
of megabytes — purely to count it.

The Go side is ``worker/internal/executor``. The two are separate
implementations of one format, and ``tests/data/frames.golden`` is read by both
suites so they cannot drift apart silently.

# What version 1 could not express, and why this exists

Version 1 terminated a request with the unframed literal bytes ``EOF`` and a
response by closing the connection.

* **A body could not contain its own terminator.** Pickle happens to always end
  with the STOP opcode, so this never fired — luck, not design.
* **A truncated response was indistinguishable from a short one.** The stream
  ended at connection close, so an executor killed mid-write read as success.
* **A raising function sent nothing at all.** There was no way to say "it ran
  and raised", so the caller inferred failure from an empty response and had to
  scrape the traceback out of captured log lines.

A length prefix settles the first two by construction. The envelope settles the
third: failure becomes a value, and it carries the traceback from the process
that actually produced it.

Note what the envelope deliberately does *not* imply. ``ok: False`` is not a
worker failure — the sandbox ran, the interpreter is healthy, and the container
is still reusable. The worker keeps reporting success and pauses it for reuse;
only the client turns the envelope into an exception.
"""

from __future__ import annotations

import struct
from typing import Any, Protocol

#: Wire format version. Recorded in the generation manifest as
#: ``executor_protocol`` so the worker can refuse a checkpoint whose executor
#: speaks something else, rather than hanging against a peer that will never
#: send a terminator it recognises.
PROTOCOL_VERSION = 2

#: Big-endian unsigned 64-bit. Big-endian because it is the wire convention and
#: reads correctly in a hex dump; 64-bit because a payload can legitimately be a
#: multi-gigabyte array and a 32-bit prefix would cap it at 4 GiB.
LENGTH_FORMAT = ">Q"
LENGTH_BYTES = struct.calcsize(LENGTH_FORMAT)

#: Refuse to allocate for a single chunk larger than this. The prefix arrives
#: from the peer, so an implausible one must be rejected before it is used as an
#: allocation size — otherwise one corrupt prefix OOM-kills the sandbox, which
#: reads as a mysterious disappearance rather than an error.
MAX_CHUNK_BYTES = 64 * 1024 * 1024

#: Refuse to accumulate a message larger than this, however it is chunked. The
#: per-chunk cap alone does not bound a peer that sends small chunks forever.
MAX_MESSAGE_BYTES = 4 * 1024 * 1024 * 1024


class ProtocolError(Exception):
    """The peer sent something this protocol cannot represent."""


class Reader(Protocol):
    """The read half of a connection. ``socket.socket`` satisfies it."""

    def recv(self, bufsize: int, /) -> bytes:
        """Return up to ``bufsize`` bytes, or empty at end of stream."""
        ...


class Writer(Protocol):
    """The write half of a connection. ``socket.socket`` satisfies it."""

    def sendall(self, data: bytes, /) -> None:
        """Write every byte of ``data``."""
        ...


def encode_length(n: int) -> bytes:
    """Encode a frame length prefix."""
    return struct.pack(LENGTH_FORMAT, n)


def decode_length(raw: bytes) -> int:
    """Decode a frame length prefix."""
    if len(raw) != LENGTH_BYTES:
        msg = f"length prefix must be {LENGTH_BYTES} bytes, got {len(raw)}"
        raise ProtocolError(msg)
    return int(struct.unpack(LENGTH_FORMAT, raw)[0])


def read_exactly(reader: Reader, n: int, *, chunk_size: int) -> bytes:
    """Read exactly ``n`` bytes, or raise.

    "Or raise" is the whole point. A stream socket returns whatever has arrived,
    so a caller that treats a short read as the complete message cannot tell a
    truncated response from a small one — which is precisely how version 1
    reported an executor killed mid-write as a success.
    """
    if n == 0:
        return b""

    buffer = bytearray()
    while len(buffer) < n:
        chunk = reader.recv(min(chunk_size, n - len(buffer)))
        if not chunk:
            msg = f"stream ended after {len(buffer)} of {n} expected bytes"
            raise ProtocolError(msg)
        buffer.extend(chunk)
    return bytes(buffer)


def read_message(
    reader: Reader,
    *,
    chunk_size: int,
    max_chunk: int = MAX_CHUNK_BYTES,
    max_bytes: int = MAX_MESSAGE_BYTES,
) -> bytes:
    """Read chunks until the zero-length terminator, and join them.

    A stream that ends before the terminator raises. That is the whole point:
    under version 1 the message ended at connection close, so an executor killed
    mid-write produced a short body indistinguishable from a small one.
    """
    parts: list[bytes] = []
    total = 0

    while True:
        length = decode_length(read_exactly(reader, LENGTH_BYTES, chunk_size=chunk_size))

        if length == 0:
            return b"".join(parts)

        # Both checked before allocating, not after.
        if length > max_chunk:
            msg = f"chunk claims {length} bytes, over the {max_chunk}-byte limit"
            raise ProtocolError(msg)
        total += length
        if total > max_bytes:
            msg = f"message exceeded {max_bytes} bytes"
            raise ProtocolError(msg)

        parts.append(read_exactly(reader, length, chunk_size=chunk_size))


def write_message(writer: Writer, payload: bytes, *, chunk_size: int) -> None:
    """Write a payload as length-prefixed chunks, then the terminator.

    Each prefix travels with its own chunk rather than as a separate send: a
    lone 8-byte write is a separate packet on some transports, and there is no
    reason to wake the reader twice per chunk.
    """
    for start in range(0, len(payload), chunk_size):
        piece = payload[start : start + chunk_size]
        writer.sendall(encode_length(len(piece)) + piece)

    # A zero-length chunk is the terminator, and it is what an empty payload
    # consists of entirely.
    writer.sendall(encode_length(0))


# ---------------------------------------------------------------------------
# The result envelope
# ---------------------------------------------------------------------------
def success_envelope(value: Any) -> dict[str, Any]:
    """Wrap a return value."""
    return {"ok": True, "value": value}


def failure_envelope(exc: BaseException, traceback_text: str) -> dict[str, Any]:
    """Wrap a raised exception.

    The exception object itself is deliberately not sent. Unpickling it on the
    client requires the class to be importable *there*, which for a library that
    only exists in the worker image it is not — so a remote ImportError would
    replace the real error with a confusing one. The type name, message and
    formatted traceback are strings, and they always arrive.
    """
    return {
        "ok": False,
        "exc_type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback_text,
    }


def validate_envelope(payload: Any) -> dict[str, Any]:
    """Check that a decoded response is a well-formed envelope.

    Called on the receiving side. A response that is not an envelope means the
    peer is speaking a different protocol version, and saying so beats a
    ``KeyError`` three frames later.
    """
    if not isinstance(payload, dict):
        msg = f"expected a result envelope, got {type(payload).__name__}"
        raise ProtocolError(msg)
    if "ok" not in payload:
        msg = "result envelope has no 'ok' field; peer may speak protocol 1"
        raise ProtocolError(msg)

    envelope: dict[str, Any] = payload
    if envelope["ok"]:
        if "value" not in envelope:
            msg = "successful envelope has no 'value' field"
            raise ProtocolError(msg)
    else:
        missing = {"exc_type", "message", "traceback"} - envelope.keys()
        if missing:
            msg = f"failure envelope is missing {', '.join(sorted(missing))}"
            raise ProtocolError(msg)
    return envelope
