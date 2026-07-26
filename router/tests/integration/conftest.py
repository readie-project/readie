"""Integration fixtures: a real router over a real channel, with a fake worker."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import grpc
import pytest
import pytest_asyncio

from crfs_router.app import App, Deps
from crfs_router.clock import FakeClock
from crfs_router.config import Settings
from crfs_router.proto import proxy_pb2_grpc, registry_pb2, registry_pb2_grpc
from tests.fakes.worker import FakeWorker


@dataclass(slots=True)
class Harness:
    """A running router plus clients for it."""

    app: App
    worker: FakeWorker
    clock: FakeClock
    proxy: proxy_pb2_grpc.ProxyServiceStub
    registry: registry_pb2_grpc.RegistryServiceStub

    async def register_worker(
        self,
        worker_id: str = "worker-1",
        *,
        status: registry_pb2.Status = registry_pb2.STATUS_READY,
        mem_total: int = 0,
    ) -> str:
        """Register a worker, exactly as the real worker does at startup."""
        uri = f"{worker_id}:50052"
        request = registry_pb2.WorkerStatus(worker_id=worker_id, worker_uri=uri, status=status)
        if mem_total and hasattr(request, "mem_total"):
            request.mem_total = mem_total
        await self.registry.PostWorkerStatus(request)
        return uri


@pytest.fixture
def settings() -> Settings:
    """Settings for a test router: ephemeral port, brisk timeouts."""
    return Settings(
        bind_host="127.0.0.1",
        port=0,
        probe_interval=3600.0,  # background sweeps are driven explicitly
        reaper_interval=3600.0,
        session_wait_timeout=5.0,
        execution_timeout=10.0,
        shutdown_grace=2.0,
        log_level="error",
        log_format="console",
    )


@pytest.fixture
def worker() -> FakeWorker:
    return FakeWorker()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest_asyncio.fixture
async def harness(
    settings: Settings, worker: FakeWorker, clock: FakeClock
) -> AsyncIterator[Harness]:
    """Run a router and yield clients wired to it."""
    app = App(settings, Deps(clock=clock, executions=worker))
    await app.start()

    channel = grpc.aio.insecure_channel(f"127.0.0.1:{app.port}")
    try:
        yield Harness(
            app=app,
            worker=worker,
            clock=clock,
            proxy=proxy_pb2_grpc.ProxyServiceStub(channel),
            registry=registry_pb2_grpc.RegistryServiceStub(channel),
        )
    finally:
        await channel.close()
        await app.stop()
