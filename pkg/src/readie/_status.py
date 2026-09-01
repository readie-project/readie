"""Maps gRPC status codes onto the error hierarchy."""

from __future__ import annotations

import grpc

from readie.errors import (
    ClusterUnavailableError,
    ExecutionCancelledError,
    InvalidRequestError,
    PermissionDeniedError,
    RemoteTimeoutError,
    ResourceExhaustedError,
    TransportError,
)

_BY_CODE: dict[grpc.StatusCode, type[TransportError]] = {
    grpc.StatusCode.UNAVAILABLE: ClusterUnavailableError,
    grpc.StatusCode.DEADLINE_EXCEEDED: RemoteTimeoutError,
    grpc.StatusCode.INVALID_ARGUMENT: InvalidRequestError,
    grpc.StatusCode.FAILED_PRECONDITION: InvalidRequestError,
    grpc.StatusCode.OUT_OF_RANGE: InvalidRequestError,
    grpc.StatusCode.RESOURCE_EXHAUSTED: ResourceExhaustedError,
    grpc.StatusCode.CANCELLED: ExecutionCancelledError,
    grpc.StatusCode.PERMISSION_DENIED: PermissionDeniedError,
    grpc.StatusCode.UNAUTHENTICATED: PermissionDeniedError,
}


def translate(error: grpc.RpcError, *, target: str) -> TransportError:
    """Turn an RPC error into the matching ``TransportError`` subclass.

    ``grpc.RpcError`` rather than ``AioRpcError`` because both transports funnel
    here: the sync ``_InactiveRpcError`` and the aio ``AioRpcError`` both expose
    ``code()`` and ``details()`` and share no other useful base.
    """
    code = error.code() if hasattr(error, "code") else None
    details = (error.details() if hasattr(error, "details") else None) or str(error)
    name = code.name if code is not None else "UNKNOWN"

    if code is grpc.StatusCode.UNAVAILABLE:
        # The most common failure by far, and the least self-explanatory: it is
        # equally "the router is down" and "the router has no worker".
        message = f"router at {target} is unavailable, or has no worker with capacity: {details}"
    else:
        message = f"{name} from {target}: {details}"

    cls = _BY_CODE.get(code, TransportError) if code is not None else TransportError
    return cls(message, code=name)
