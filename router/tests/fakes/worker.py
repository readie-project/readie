"""A programmable stand-in for the Go worker.

It implements ``ExecutionClient`` directly rather than serving real gRPC. That
keeps the integration tests to one real server — the router's own — so a test
exercises the router's transport without also depending on a second server's
timing.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import grpc

from crfs_router.proto import execution_pb2
from crfs_router.workers.ports import ExecutionStream


@dataclass(slots=True)
class RecordedCall:
    """One execution the fake worker was asked to run."""

    target: str
    requests: list[execution_pb2.WorkerExecutionRequest] = field(default_factory=list)
    cancelled: bool = False

    @property
    def header(self) -> execution_pb2.WorkerExecutionRequest:
        """The first message, which carries the placement."""
        return self.requests[0]

    @property
    def payload(self) -> bytes:
        """Every payload chunk, concatenated."""
        return b"".join(r.payload for r in self.requests)


class FakeWorker:
    """Builds fake execution streams and records what was sent to them."""

    def __init__(
        self,
        *,
        reply: bytes = b"result",
        logs: tuple[str, ...] = (),
        success: bool = True,
        container_id: str | None = None,
        checkpoint_id: str = "",
        error: grpc.aio.AioRpcError | None = None,
        response_delay: float = 0.0,
        silent: bool = False,
    ) -> None:
        self.reply = reply
        self.logs = logs
        self.success = success
        #: When None, a fresh container id is minted per call, which is what a
        #: real worker does for a cold start.
        self.container_id = container_id
        self.checkpoint_id = checkpoint_id
        self.error = error
        self.response_delay = response_delay
        #: Produce no output at all. This is the real worker's behaviour when
        #: the user's function raises: the executor prints the traceback and
        #: sends nothing.
        self.silent = silent

        self.calls: list[RecordedCall] = []
        self._ids = itertools.count(1)

    def open(self, target: str, *, timeout: float | None = None) -> ExecutionStream:
        """Begin a fake execution stream."""
        call = RecordedCall(target=target)
        self.calls.append(call)
        return _FakeStream(self, call)

    def next_container_id(self) -> str:
        """Return the container id the next call will report."""
        if self.container_id is not None:
            return self.container_id
        return f"exec_container-{next(self._ids)}"


class _FakeStream:
    """One fake bidirectional call."""

    def __init__(self, worker: FakeWorker, call: RecordedCall) -> None:
        self._worker = worker
        self._call = call
        self._writes_done = asyncio.Event()
        self._container_id = worker.next_container_id()

    async def write(self, request: execution_pb2.WorkerExecutionRequest) -> None:
        """Record a request message."""
        self._call.requests.append(request)

    async def done_writing(self) -> None:
        """Note that the client half is complete."""
        self._writes_done.set()

    def cancel(self) -> bool:
        """Abort the call."""
        self._call.cancelled = True
        return True

    async def __aiter__(self) -> AsyncIterator[execution_pb2.WorkerExecutionResponse]:
        """Yield the programmed responses.

        Waits for the request half first, mirroring the real worker: it streams
        the whole body to the executor before the executor replies.
        """
        await self._writes_done.wait()

        if self._worker.error is not None:
            raise self._worker.error
        if self._worker.response_delay:
            await asyncio.sleep(self._worker.response_delay)
        if self._worker.silent:
            return

        header = self._call.requests[0] if self._call.requests else None
        request_id = header.request_id if header else ""
        session_id = header.session_id if header else ""

        def base() -> execution_pb2.WorkerExecutionResponse:
            return execution_pb2.WorkerExecutionResponse(
                request_id=request_id,
                session_id=session_id,
                worker_id=header.worker_id if header else "",
                container_id=self._container_id,
                checkpoint_id=self._worker.checkpoint_id,
                success=self._worker.success,
            )

        for line in self._worker.logs:
            message = base()
            message.logs = line
            yield message

        message = base()
        message.payload = self._worker.reply
        yield message
