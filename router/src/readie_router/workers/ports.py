"""The seams the router uses to reach a worker.

Declared here, on the consumer's side, and structurally: the production
implementation wraps a generated stub we do not control and cannot make inherit
from a base class of ours.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Protocol

from readie_router.proto import execution_pb2


class ExecutionStream(Protocol):
    """A live bidirectional call to a worker."""

    def __aiter__(self) -> AsyncIterator[execution_pb2.WorkerExecutionResponse]:
        """Iterate the worker's responses."""
        ...

    async def write(self, request: execution_pb2.WorkerExecutionRequest) -> None:
        """Send one request message."""
        ...

    async def done_writing(self) -> None:
        """Signal that no further requests will be sent."""
        ...

    def cancel(self) -> bool:
        """Abort the call. Synchronous, so it is safe in a ``finally``."""
        ...


class ExecutionClient(Protocol):
    """Opens execution calls against workers."""

    def open(self, target: str, *, timeout: float | None = None) -> ExecutionStream:
        """Begin a bidirectional execution stream to a worker."""
        ...


class HealthClient(Protocol):
    """Probes a worker's liveness.

    Separate from ``ExecutionClient`` because it is used by a background task
    with a different failure policy: a probe failing is normal and counted,
    whereas an execution failing is an error to surface.
    """

    async def check(self, target: str, *, timeout: float) -> bool:  # noqa: ASYNC109 - the deadline belongs to the gRPC call, not a wrapper
        """Return whether the worker reports itself serving."""
        ...


__all__ = ["ExecutionClient", "ExecutionStream", "HealthClient", "Iterable"]
