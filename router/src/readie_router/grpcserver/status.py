"""Mapping domain errors onto gRPC status codes.

One function, at the transport boundary, so the scheduling layer never imports
gRPC and every handler reports the same code for the same condition.
"""

from __future__ import annotations

import grpc

from readie_router.errors import (
    InvalidRequestError,
    NoCapacityError,
    NoWorkersRegisteredError,
    SessionBusyError,
    WorkerUnavailableError,
)

#: Codes that mean "try again", as opposed to "this request is wrong".
_MAPPING: tuple[tuple[type[BaseException], grpc.StatusCode, str], ...] = (
    (InvalidRequestError, grpc.StatusCode.INVALID_ARGUMENT, "invalid execution request"),
    (NoWorkersRegisteredError, grpc.StatusCode.UNAVAILABLE, "no workers are available"),
    (NoCapacityError, grpc.StatusCode.RESOURCE_EXHAUSTED,
     "no worker can accept this request"),
    (SessionBusyError, grpc.StatusCode.RESOURCE_EXHAUSTED, "the session is busy"),
    (WorkerUnavailableError, grpc.StatusCode.UNAVAILABLE,
     "the assigned worker is unreachable"),
    (TimeoutError, grpc.StatusCode.DEADLINE_EXCEEDED, "the execution timed out"),
)


def to_status(exc: BaseException) -> tuple[grpc.StatusCode, str]:
    """Return the code and client-facing message for an error.

    Messages stay generic on purpose. The specifics belong in the log line
    keyed by ``request_id``, not on a wire the router does not control - a
    client has no use for a worker's internal address, and leaking it invites
    it to be depended upon.
    """
    if isinstance(exc, grpc.aio.AioRpcError):
        # A failure from the worker keeps its own code: a worker reporting
        # RESOURCE_EXHAUSTED means something different from the router being
        # unable to place the request at all.
        return exc.code(), "the worker could not complete the execution"

    for exc_type, code, message in _MAPPING:
        if isinstance(exc, exc_type):
            return code, message

    return grpc.StatusCode.INTERNAL, "internal router error"
