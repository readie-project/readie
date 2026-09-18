"""The client-facing bridge.

Relays one client stream to one worker stream and back. Everything that makes
this correct is about lifetimes: the placement must be released exactly once,
the worker call must be cancelled if the client disappears, and the session's
turn must be given back even when the handler is torn down abruptly.

Written as a coroutine using ``context.write`` rather than as an async
generator. A generator being closed raises ``GeneratorExit`` at the yield
point, which makes awaiting during cleanup fraught; a coroutine can hold a
``TaskGroup`` and can await in ``finally``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

import grpc
import structlog

from readie_router.errors import InvalidRequestError, RouterError
from readie_router.grpcserver.session_gate import SessionGate
from readie_router.grpcserver.status import to_status
from readie_router.logging import (
    KEY_CHECKPOINT_ID,
    KEY_CONTAINER_ID,
    KEY_ERROR,
    KEY_REQUEST_ID,
    KEY_SESSION_ID,
    KEY_WORKER_ID,
)
from readie_router.proto import execution_pb2, proxy_pb2, proxy_pb2_grpc, resources_pb2
from readie_router.scheduling.models import (
    RESOURCE_GPU_MEMORY,
    RESOURCE_MEMORY,
    Budget,
    Demand,
    Outcome,
    Placement,
)
from readie_router.scheduling.scheduler import ProvisionRequest, Scheduler
from readie_router.workers.ports import ExecutionClient, ExecutionStream


@dataclass(slots=True)
class _Relay:
    """Mutable per-request bookkeeping shared by the two pumps."""

    bound: bool = False
    responses: int = 0
    payload_bytes: int = 0


class ProxyService(proxy_pb2_grpc.ProxyServiceServicer):
    """Accepts client executions and relays them to a worker."""

    def __init__(
        self,
        scheduler: Scheduler,
        executions: ExecutionClient,
        gate: SessionGate,
        *,
        execution_timeout: float,
        default_memory: int,
    ) -> None:
        self._scheduler = scheduler
        self._executions = executions
        self._gate = gate
        self._execution_timeout = execution_timeout
        self._default_memory = default_memory
        self._log = structlog.get_logger("grpcserver.proxy")

    async def RequestExecution(  # noqa: N802 - the generated interface names it
        self,
        request_iterator: grpc.aio.MessageIterator,
        context: grpc.aio.ServicerContext,
    ) -> None:
        """Run one execution on the client's behalf."""
        try:
            header = await self._read_header(request_iterator, context)
        except InvalidRequestError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            return

        log = self._log.bind(
            **{KEY_REQUEST_ID: header.request_id, KEY_SESSION_ID: header.session_id}
        )

        try:
            # An empty session_id means "not part of any session" - there is
            # no affinity to read and no one else who could ever share this
            # key, so gating it would only make every such call queue behind
            # every other one on the same "" lock for nothing.
            if header.session_id:
                # Serialise per session before placing: the placement decision
                # reads the session's affinity, and a concurrent request must
                # not observe it mid-flight.
                async with self._gate.hold(header.session_id):
                    await self._run(header, request_iterator, context, log)
            else:
                await self._run(header, request_iterator, context, log)
        except BaseExceptionGroup as group:
            # The relay runs its two pumps under a TaskGroup, which reports any
            # failure as a group. Without unwrapping, a perfectly classifiable
            # worker error would reach the client as UNKNOWN.
            failure = _representative(group)
            code, message = to_status(failure)
            log.warning("execution failed", **{KEY_ERROR: str(failure)}, code=code.name)
            await context.abort(code, message)
        except (RouterError, grpc.aio.AioRpcError) as exc:
            code, message = to_status(exc)
            log.warning("execution rejected", **{KEY_ERROR: str(exc)}, code=code.name)
            await context.abort(code, message)

    async def _read_header(
        self,
        request_iterator: grpc.aio.MessageIterator,
        context: grpc.aio.ServicerContext,  # noqa: ARG002
    ) -> proxy_pb2.ClientExecutionRequest:
        """Consume and validate the first message.

        The previous implementation returned an empty, successful stream when
        the first message was not a config header, so a client that got the
        protocol wrong saw an empty result rather than an error.
        """
        try:
            header: proxy_pb2.ClientExecutionRequest = await anext(aiter(request_iterator))
        except StopAsyncIteration as exc:
            msg = "the request stream closed before sending anything"
            raise InvalidRequestError(msg) from exc

        if header.WhichOneof("data") != "config":
            msg = "the first message must carry the execution config"
            raise InvalidRequestError(msg)
        if not header.request_id:
            msg = "request_id is required"
            raise InvalidRequestError(msg)
        return header

    async def _run(
        self,
        header: proxy_pb2.ClientExecutionRequest,
        request_iterator: grpc.aio.MessageIterator,
        context: grpc.aio.ServicerContext,
        log: structlog.stdlib.BoundLogger,
    ) -> None:
        """Place the request, then relay it."""
        placement = self._scheduler.provision(
            ProvisionRequest(
                request_id=header.request_id,
                session_id=header.session_id,
                demand=self._to_demand(header.config),
            )
        )
        log = log.bind(
            **{KEY_WORKER_ID: placement.worker_id, KEY_CHECKPOINT_ID: placement.checkpoint_id, KEY_CONTAINER_ID: placement.container_id},
            warm=placement.warm,
        )
        log.info("execution placed")

        outcome = Outcome.FAILURE  # zero value: fail safe, as the worker does
        relay = _Relay()
        try:
            await self._relay(
                placement,
                request_iterator=request_iterator,
                context=context,
                relay=relay,
                log=log,
            )
            outcome = Outcome.SUCCESS
        finally:
            # Synchronous, so it cannot deadlock or be skipped on teardown.
            self._scheduler.release(placement.request_id, outcome)
            log.info(
                "execution finished",
                outcome=outcome.name.lower(),
                responses=relay.responses,
                payload_bytes=relay.payload_bytes,
            )

    async def _relay(
        self,
        placement: Placement,
        *,
        request_iterator: grpc.aio.MessageIterator,
        context: grpc.aio.ServicerContext,
        relay: _Relay,
        log: structlog.stdlib.BoundLogger,
    ) -> None:
        """Pump the client stream to the worker and the worker's back."""
        stream = self._executions.open(placement.worker_uri, timeout=self._execution_timeout)
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._pump_requests(placement, request_iterator, stream))
                tasks.create_task(
                    self._pump_responses(placement, stream, context=context, relay=relay, log=log)
                )
        finally:
            # Cancel is synchronous. If the client vanished mid-stream the
            # worker call would otherwise run to completion unobserved.
            stream.cancel()

    async def _pump_requests(
        self,
        placement: Placement,
        request_iterator: grpc.aio.MessageIterator,
        stream: ExecutionStream,
    ) -> None:
        """Forward the client's payload chunks to the worker.

        The first message the worker sees carries the placement and the resource
        budgets. The worker reads its provisioning fields off the first message
        only and treats the rest as payload, and it tolerates an empty first
        chunk, so sending an explicit header keeps the mapping from client
        messages to worker messages one-to-one.
        """
        await stream.write(_worker_header(placement))

        async for message in request_iterator:
            if message.WhichOneof("data") != "payload":
                # A second config mid-stream is not part of the protocol. Skip
                # it rather than forwarding an empty payload frame, which is what
                # the previous implementation did.
                continue
            await stream.write(_worker_chunk(placement, message.payload))

        await stream.done_writing()

    async def _pump_responses(
        self,
        placement: Placement,
        stream: ExecutionStream,
        *,
        context: grpc.aio.ServicerContext,
        relay: _Relay,
        log: structlog.stdlib.BoundLogger,
    ) -> None:
        """Forward the worker's output to the client."""
        async for response in stream:
            if not relay.bound:
                relay.bound = True
                # The worker tells us which container it actually used, which
                # for a cold start is the first time anyone knows.
                self._scheduler.bind(
                    placement.request_id,
                    response.worker_id or placement.worker_id,
                    response.container_id,
                    response.checkpoint_id,
                )
                if response.container_id and response.container_id != placement.container_id:
                    log.debug("bound to container", **{KEY_CONTAINER_ID: response.container_id})

            relay.responses += 1
            kind = response.WhichOneof("data")
            if kind == "payload":
                relay.payload_bytes += len(response.payload)
            elif kind is None:
                # A provisioning-only frame carries no output; it has already
                # done its job above.
                continue

            await context.write(_client_response(placement, response))

    def _to_demand(self, config: proxy_pb2.ExecutionConfig) -> Demand:
        """Turn the client's config into a demand, applying per-kind defaults.

        A memory budget the client omits or leaves at 0 gets the cluster
        default; other kinds are forwarded as sent. Import hints are deduped and
        sorted for a stable worker-facing order.
        """
        budgets: list[Budget] = []
        has_memory = False
        for wire in config.budgets:
            alloc = wire.alloc
            if wire.kind == RESOURCE_MEMORY:
                has_memory = True
                if alloc <= 0:
                    alloc = self._default_memory
            budgets.append(Budget(kind=wire.kind, alloc=alloc, max=wire.max))
        if not has_memory:
            budgets.append(Budget(kind=RESOURCE_MEMORY, alloc=self._default_memory, max=0))

        imports = tuple(sorted({name for name in config.imports if name}))
        # A request is GPU if it says so or if it carries any GPU-memory budget.
        gpu = config.gpu or any(b.kind == RESOURCE_GPU_MEMORY for b in budgets)
        return Demand(
            budgets=tuple(budgets),
            resources=imports,
            flavor="gpu" if gpu else "cpu",
        )


def _representative(group: BaseExceptionGroup) -> BaseException:
    """Pick the most informative leaf of a (possibly nested) exception group.

    A worker failure and the cancellation it causes in the sibling pump arrive
    together; the worker failure is the one worth reporting, so cancellations
    are considered last.
    """
    leaves: list[BaseException] = []

    def walk(exc: BaseException) -> None:
        if isinstance(exc, BaseExceptionGroup):
            for nested in exc.exceptions:
                walk(nested)
        else:
            leaves.append(exc)

    walk(group)

    for leaf in leaves:
        if isinstance(leaf, RouterError | grpc.aio.AioRpcError):
            return leaf
    for leaf in leaves:
        if not isinstance(leaf, asyncio.CancelledError):
            return leaf
    return leaves[0] if leaves else group


def _worker_budgets(placement: Placement) -> list[resources_pb2.ResourceBudget]:
    """The placement's budgets as wire messages, for the worker."""
    return [
        resources_pb2.ResourceBudget(
            # kind is a RESOURCE_* int; the proto enum agrees by value.
            kind=cast("resources_pb2.ResourceKind", b.kind),
            alloc=b.alloc,
            max=b.max,
        )
        for b in placement.budgets
    ]


def _worker_header(placement: Placement) -> execution_pb2.WorkerExecutionRequest:
    """Build the first message sent to the worker.

    ``resources`` (import hints) and ``budgets`` are populated here: the worker
    reads its provisioning fields off this first message and uses the imports
    for checkpoint selection.
    """
    return execution_pb2.WorkerExecutionRequest(
        request_id=placement.request_id,
        session_id=placement.session_id,
        worker_id=placement.worker_id,
        container_id=placement.container_id,
        checkpoint_id=placement.checkpoint_id,
        budgets=_worker_budgets(placement),
        resources=list(placement.resources),
        payload=b"",
    )


def _worker_chunk(placement: Placement, payload: bytes) -> execution_pb2.WorkerExecutionRequest:
    """Build a payload message for the worker."""
    return execution_pb2.WorkerExecutionRequest(
        request_id=placement.request_id,
        session_id=placement.session_id,
        worker_id=placement.worker_id,
        container_id=placement.container_id,
        checkpoint_id=placement.checkpoint_id,
        budgets=_worker_budgets(placement),
        payload=payload,
    )


def _client_response(
    placement: Placement, response: execution_pb2.WorkerExecutionResponse
) -> proxy_pb2.ClientExecutionResponse:
    """Translate a worker response for the client.

    ``success`` is carried through. The previous implementation never set it,
    so every client response claimed failure regardless of what happened.

    Attribution is forwarded too: a client cannot choose a worker, but knowing
    which one ran a slow call is the difference between "the platform is slow"
    and "one worker is slow".
    """
    message = proxy_pb2.ClientExecutionResponse(
        request_id=response.request_id or placement.request_id,
        session_id=response.session_id or placement.session_id,
        success=response.success,
        worker_id=response.worker_id or placement.worker_id,
        container_id=response.container_id or placement.container_id,
    )
    kind = response.WhichOneof("data")
    if kind == "payload":
        message.payload = response.payload
    elif kind == "logs":
        message.logs = response.logs
    return message
