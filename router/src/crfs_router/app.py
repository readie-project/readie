"""The composition root.

The only module that constructs concrete types. Everything below it receives
its collaborators through ``__init__`` and reaches for nothing — which is what
lets the integration tests swap the worker client for a fake without touching
any other file, and what makes the old ``get_scheduler()`` global unnecessary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import TracebackType

import structlog

from crfs_router.clock import Clock, MonotonicClock
from crfs_router.config import Settings
from crfs_router.grpcserver.proxy_service import ProxyService
from crfs_router.grpcserver.registry_service import RegistryService
from crfs_router.grpcserver.server import RouterServer
from crfs_router.grpcserver.session_gate import SessionGate
from crfs_router.logging import KEY_ERROR
from crfs_router.scheduling.policy import default_selector
from crfs_router.scheduling.reaper import Reaper, ReaperPolicy
from crfs_router.scheduling.scheduler import Scheduler
from crfs_router.scheduling.state import ClusterState
from crfs_router.workers.channels import WorkerChannelPool
from crfs_router.workers.client import GrpcExecutionClient, GrpcHealthClient
from crfs_router.workers.ports import ExecutionClient, HealthClient
from crfs_router.workers.prober import WorkerProber


@dataclass(slots=True)
class Deps:
    """The seams tests replace.

    Defaults build the real thing, so production passes ``Deps()``.
    """

    clock: Clock = field(default_factory=MonotonicClock)
    executions: ExecutionClient | None = None
    health: HealthClient | None = None


class App:
    """A fully wired router."""

    def __init__(self, settings: Settings, deps: Deps | None = None) -> None:
        deps = deps or Deps()
        self._settings = settings
        self._clock = deps.clock
        self._log = structlog.get_logger("app")

        self._state = ClusterState()
        self._scheduler = Scheduler(
            state=self._state,
            selector=default_selector(memory_headroom=settings.memory_headroom),
            clock=self._clock,
        )

        self._pool = WorkerChannelPool(max_message_bytes=settings.max_message_bytes)
        self._executions = deps.executions or GrpcExecutionClient(self._pool)
        health = deps.health or GrpcHealthClient(self._pool)

        self._reaper = Reaper(
            state=self._state,
            scheduler=self._scheduler,
            clock=self._clock,
            policy=ReaperPolicy(
                worker_ttl=settings.worker_ttl,
                executor_ttl=settings.executor_ttl,
                executor_error_ttl=settings.executor_error_ttl,
                session_ttl=settings.session_ttl,
                lease_ttl=settings.lease_ttl,
                max_sessions=settings.max_sessions,
            ),
        )
        self._prober = WorkerProber(
            state=self._state,
            health=health,
            clock=self._clock,
            interval=settings.probe_interval,
            timeout=settings.probe_timeout,
            failure_threshold=settings.probe_failure_threshold,
        )

        self._server = RouterServer(
            proxy=ProxyService(
                scheduler=self._scheduler,
                executions=self._executions,
                gate=SessionGate(wait_timeout=settings.session_wait_timeout),
                execution_timeout=settings.execution_timeout,
                default_memory=settings.default_memory,
            ),
            registry=RegistryService(state=self._state, clock=self._clock),
            listen_addr=settings.listen_addr,
            max_concurrent_rpcs=settings.max_concurrent_rpcs,
            max_message_bytes=settings.max_message_bytes,
        )

        self._background: list[asyncio.Task[None]] = []

    @property
    def port(self) -> int:
        """The bound port; meaningful after ``start``."""
        return self._server.port

    @property
    def state(self) -> ClusterState:
        """The cluster registry. Exposed for tests and diagnostics."""
        return self._state

    async def start(self) -> None:
        """Bind, start background work, and begin serving."""
        await self._server.start()

        self._background = [
            asyncio.create_task(self._prober.run(), name="prober"),
            asyncio.create_task(self._reap_loop(), name="reaper"),
            asyncio.create_task(self._channel_gc_loop(), name="channel-gc"),
        ]

        self._server.set_serving(serving=True)
        self._log.info(
            "router ready",
            addr=self._settings.advertised_addr,
            port=self._server.port,
        )

    async def run(self, stop: asyncio.Event) -> None:
        """Serve until ``stop`` is set, then shut down."""
        await self.start()
        try:
            await stop.wait()
            self._log.info("shutdown signal received")
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Drain and release everything, in reverse order of acquisition."""
        for task in self._background:
            task.cancel()
        for task in self._background:
            try:
                await task
            except asyncio.CancelledError:
                pass  # expected: we just cancelled it
            except Exception as exc:
                # One bad task must not stop the others being awaited.
                self._log.warning(
                    "background task failed during shutdown",
                    task=task.get_name(),
                    **{KEY_ERROR: str(exc)},
                )
        self._background = []

        await self._server.stop(self._settings.shutdown_grace)
        await self._pool.close()
        self._log.info("router shut down")

    async def _reap_loop(self) -> None:
        """Sweep expired state on a fixed interval."""
        while True:
            await asyncio.sleep(self._settings.reaper_interval)
            try:
                result = self._reaper.sweep()
            except Exception as exc:
                self._log.warning("reaper sweep failed", **{KEY_ERROR: str(exc)})
                continue
            if not result.is_empty:
                self._log.info(
                    "reclaimed expired state",
                    workers=result.workers,
                    executors=result.executors,
                    sessions=result.sessions,
                    leases=result.leases,
                    evicted_by_cap=result.evicted_by_cap,
                )

    async def _channel_gc_loop(self) -> None:
        """Close channels to workers that have been evicted.

        Eviction happens inside the synchronous domain, which cannot await, so
        it queues the address and this drains the queue.
        """
        while True:
            await asyncio.sleep(self._settings.reaper_interval)
            for uri in self._state.drain_evicted_uris():
                await self._pool.evict(uri)

    async def __aenter__(self) -> App:
        """Start the router."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop the router."""
        await self.stop()
