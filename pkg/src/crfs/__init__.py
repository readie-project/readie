"""Run Python functions on checkpoint-restore serverless workers.

    from crfs import remote

    @remote
    def add(a, b):
        return a + b

    add(1, 2)             # blocking
    await add.aio(1, 2)   # from an event loop

The decorated function is cloudpickled and executed by a Python interpreter
inside a gVisor sandbox on a worker, so it must be picklable and its imports must
exist in the worker's image.
"""

from __future__ import annotations

from crfs.client import Client, Session
from crfs.codec import CloudpickleCodec, ResultCodec
from crfs.config import Settings
from crfs.decorator import RemoteFunction, configure, default_client, remote, reset
from crfs.errors import (
    BlockingCallInEventLoopError,
    ClientClosedError,
    ClusterUnavailableError,
    ConfigurationError,
    CrfsError,
    EmptyResultError,
    ExecutionCancelledError,
    ExecutionError,
    InvalidRequestError,
    PermissionDeniedError,
    RemoteExecutionError,
    RemoteTimeoutError,
    ResourceExhaustedError,
    SerializationError,
    TransportError,
)
from crfs.protocol import CallRef, Outcome
from crfs.resources import (
    AstEstimator,
    Estimate,
    Import,
    NullEstimator,
    ResourceEstimator,
    Variable,
)
from crfs.transport import AsyncTransport, Transport

__version__ = "0.1.0"

__all__ = [
    "AstEstimator",
    "AsyncTransport",
    "BlockingCallInEventLoopError",
    "CallRef",
    "Client",
    "ClientClosedError",
    "CloudpickleCodec",
    "ClusterUnavailableError",
    "ConfigurationError",
    "CrfsError",
    "EmptyResultError",
    "Estimate",
    "ExecutionCancelledError",
    "ExecutionError",
    "Import",
    "InvalidRequestError",
    "NullEstimator",
    "Outcome",
    "PermissionDeniedError",
    "RemoteExecutionError",
    "RemoteFunction",
    "RemoteTimeoutError",
    "ResourceEstimator",
    "ResourceExhaustedError",
    "ResultCodec",
    "SerializationError",
    "Session",
    "Settings",
    "Transport",
    "TransportError",
    "Variable",
    "__version__",
    "configure",
    "default_client",
    "remote",
    "reset",
]
