"""The worker-facing registry.

Workers push their lifecycle and load here. Every handler is a thin adapter:
validate, translate, hand to the domain, acknowledge.

Two things differ from the previous implementation beyond the bug fixes. The
handlers subclass the generated ``*Servicer`` base rather than the client stub -
which had worked only because the registration helper duck-types on method
names, and which lost the UNIMPLEMENTED default for anything not overridden.
And they are ``async def``: they run on an aio server, where a synchronous
handler blocks the loop and cannot ``await context.abort``.
"""

from __future__ import annotations

import grpc
import structlog

from readie_router.clock import Clock
from readie_router.logging import KEY_CONTAINER_ID, KEY_WORKER_ID
from readie_router.proto import registry_pb2, registry_pb2_grpc
from readie_router.scheduling.state import ClusterState


class RegistryService(registry_pb2_grpc.RegistryServiceServicer):
    """Records what workers report about themselves."""

    def __init__(self, state: ClusterState, clock: Clock) -> None:
        self._state = state
        self._clock = clock
        self._log = structlog.get_logger("grpcserver.registry")

    async def PostWorkerStatus(  # noqa: N802 - the generated interface names it
        self,
        request: registry_pb2.WorkerStatus,
        context: grpc.aio.ServicerContext,
    ) -> registry_pb2.RegistryUpdateResponse:
        """Record a worker's lifecycle state.

        This is also registration: the first READY from a worker is what tells
        the router the worker exists and where to reach it. Capacity rides on
        every status, not just the first, so a router that restarts relearns it
        from the next report rather than scheduling blind.
        """
        if not request.worker_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "worker_id is required")

        self._state.apply_worker_status(
            worker_id=request.worker_id,
            worker_uri=request.worker_uri,
            status=request.status,
            now=self._clock.now(),
            mem_total=request.mem_total,
            max_executors=request.max_executors,
            flavor=request.flavor,
            gpu_mem_total=request.gpu_mem_total,
        )
        self._log.info(
            "worker status",
            **{KEY_WORKER_ID: request.worker_id},
            worker_uri=request.worker_uri,
            status=registry_pb2.Status.Name(request.status),
        )
        return registry_pb2.RegistryUpdateResponse(updated=True)

    async def PostExecutorStatus(  # noqa: N802
        self,
        request: registry_pb2.ExecutorStatus,
        context: grpc.aio.ServicerContext,
    ) -> registry_pb2.RegistryUpdateResponse:
        """Record a container's lifecycle state.

        This is the reliable half of session binding. The proxy stream only
        reveals a container id if the execution produces output, so an
        execution that returns nothing would otherwise never bind its session.
        """
        if not request.worker_id or not request.container_id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "worker_id and container_id are required"
            )

        self._state.apply_executor_status(
            worker_id=request.worker_id,
            container_id=request.container_id,
            session_id=request.session_id,
            request_id=request.request_id,
            status=request.status,
            now=self._clock.now(),
        )
        self._log.info(
            "executor status",
            **{KEY_WORKER_ID: request.worker_id, KEY_CONTAINER_ID: request.container_id},
            status=registry_pb2.Status.Name(request.status),
        )
        return registry_pb2.RegistryUpdateResponse(updated=True)

    async def PostWorkerUtilization(  # noqa: N802
        self,
        request: registry_pb2.WorkerUtilization,
        context: grpc.aio.ServicerContext,
    ) -> registry_pb2.RegistryUpdateResponse:
        """Record a worker's load.

        The previous implementation indexed ``request.container_id`` - a field
        this message does not have - so every call raised AttributeError and
        worker-level load stayed permanently zero.
        """
        if not request.worker_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "worker_id is required")

        self._state.apply_worker_utilization(
            worker_id=request.worker_id,
            now=self._clock.now(),
            cpu_util=request.cpu_util,
            cpu_total=request.cpu_total,
            gpu_util=request.gpu_util,
            gpu_total=request.gpu_total,
            mem_used=request.mem_used,
            mem_total=request.mem_total,
            executor_count=request.executor_count,
            gpu_mem_used=request.gpu_mem_used,
            gpu_mem_total=request.gpu_mem_total,
        )
        self._log.debug("worker utilization", **{KEY_WORKER_ID: request.worker_id})
        return registry_pb2.RegistryUpdateResponse(updated=True)

    async def PostExecutorUtilization(  # noqa: N802
        self,
        request: registry_pb2.ExecutorUtilization,
        context: grpc.aio.ServicerContext,
    ) -> registry_pb2.RegistryUpdateResponse:
        """Record one container's load."""
        if not request.worker_id or not request.container_id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "worker_id and container_id are required"
            )

        self._state.apply_executor_utilization(
            worker_id=request.worker_id,
            container_id=request.container_id,
            now=self._clock.now(),
            cpu_util=request.cpu_util,
            cpu_total=request.cpu_total,
            gpu_util=request.gpu_util,
            gpu_total=request.gpu_total,
        )
        self._log.debug(
            "executor utilization",
            **{KEY_WORKER_ID: request.worker_id, KEY_CONTAINER_ID: request.container_id},
        )
        return registry_pb2.RegistryUpdateResponse(updated=True)
