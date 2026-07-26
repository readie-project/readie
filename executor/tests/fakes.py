"""Byte-stream doubles, so framing is testable without a socket."""

from __future__ import annotations


class ScriptedReader:
    """Hands back a fixed sequence of recv results, then end-of-stream.

    Scripting the *chunk boundaries* is the point: a stream socket does not
    preserve write boundaries, and the framing bugs worth testing are all about
    where the reads happen to split.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.calls = 0

    def recv(self, bufsize: int, /) -> bytes:
        self.calls += 1
        if not self.chunks:
            return b""
        head = self.chunks[0]
        if len(head) <= bufsize:
            return self.chunks.pop(0)
        self.chunks[0] = head[bufsize:]
        return head[:bufsize]


def dribbling(payload: bytes) -> ScriptedReader:
    """A reader that yields exactly one byte per recv."""
    return ScriptedReader([payload[i : i + 1] for i in range(len(payload))])


class RecordingWriter:
    """Collects everything written, and how it was split."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def sendall(self, data: bytes, /) -> None:
        self.writes.append(bytes(data))

    @property
    def data(self) -> bytes:
        return b"".join(self.writes)


class BrokenWriter:
    """Fails on the nth write, as a peer hanging up mid-response does."""

    def __init__(self, fail_after: int = 0) -> None:
        self.fail_after = fail_after
        self.writes = 0

    def sendall(self, data: bytes, /) -> None:
        if self.writes >= self.fail_after:
            raise BrokenPipeError("peer hung up")
        self.writes += 1
