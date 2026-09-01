"""gRPC implementations of the worker-facing ports."""

from __future__ import annotations

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

from readie_router.proto import execution_pb2_grpc
from readie_router.workers.channels import WorkerChannelPool
from readie_router.workers.ports import ExecutionStream


class GrpcExecutionClient:
    """Opens execution streams over pooled channels."""

    def __init__(self, pool: WorkerChannelPool) -> None:
        self._pool = pool

    def open(self, target: str, *, timeout: float | None = None) -> ExecutionStream:
        """Begin a bidirectional execution stream to a worker.

        The stub call object already satisfies ``ExecutionStream``; returning it
        directly avoids a wrapper that would have to forward four methods and
        get the cancellation semantics exactly right.
        """
        stub = execution_pb2_grpc.ExecutionServiceStub(self._pool.get(target))
        stream: ExecutionStream = stub.RequestExecution(timeout=timeout)
        return stream


class GrpcHealthClient:
    """Probes workers over pooled channels.

    Probing through the same channel the proxy will use is deliberate: it tests
    the path that matters rather than a proxy for it, so a channel that is
    wedged shows up as a failed probe.
    """

    def __init__(self, pool: WorkerChannelPool) -> None:
        self._pool = pool

    async def check(self, target: str, *, timeout: float) -> bool:  # noqa: ASYNC109 - the deadline belongs to the gRPC call, not a wrapper
        """Return whether the worker reports itself serving."""
        stub = health_pb2_grpc.HealthStub(self._pool.get(target))
        try:
            response = await stub.Check(health_pb2.HealthCheckRequest(), timeout=timeout)
        except grpc.aio.AioRpcError:
            return False
        except RuntimeError:
            # The pool closed underneath us during shutdown.
            return False
        return bool(response.status == health_pb2.HealthCheckResponse.SERVING)
