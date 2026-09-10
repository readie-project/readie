"""Run Python functions on checkpoint-restore serverless workers.

    from readie import remote
    import asyncio


    @remote
    def add(a, b):
        return a + b

    print(add(1, 2))  # blocking

    async def add_async(a, b):
        return await add.aio(a, b)

    print(asyncio.run(add_async(1, 2)))  # from an event loop

The decorated function is cloudpickled and executed by a Python interpreter
inside a gVisor sandbox on a worker, so it must be picklable and its imports must
exist in the worker's image.
"""

from __future__ import annotations

from readie.budget import Budget, ResourceKind
from readie.client import Client, Session
from readie.codec import CloudpickleCodec, ResultCodec
from readie.config import Settings
from readie.decorator import RemoteFunction, configure, default_client, remote, reset
from readie.errors import (
    BlockingCallInEventLoopError,
    ClientClosedError,
    ClusterUnavailableError,
    ConfigurationError,
    EmptyResultError,
    ExecutionCancelledError,
    ExecutionError,
    InvalidPackageError,
    InvalidRequestError,
    PermissionDeniedError,
    ReadieError,
    RemoteExecutionError,
    RemoteTimeoutError,
    ResourceExhaustedError,
    SerializationError,
    TransportError,
)
from readie.protocol import CallRef, Outcome
from readie.transport import AsyncTransport, Transport

__version__ = "0.1.0"

__all__ = [
    "AsyncTransport",
    "BlockingCallInEventLoopError",
    "Budget",
    "CallRef",
    "Client",
    "ClientClosedError",
    "CloudpickleCodec",
    "ClusterUnavailableError",
    "ConfigurationError",
    "EmptyResultError",
    "ExecutionCancelledError",
    "ExecutionError",
    "InvalidPackageError",
    "InvalidRequestError",
    "Outcome",
    "PermissionDeniedError",
    "ReadieError",
    "RemoteExecutionError",
    "RemoteFunction",
    "RemoteTimeoutError",
    "ResourceExhaustedError",
    "ResourceKind",
    "ResultCodec",
    "SerializationError",
    "Session",
    "Settings",
    "Transport",
    "TransportError",
    "__version__",
    "configure",
    "default_client",
    "remote",
    "reset",
]
