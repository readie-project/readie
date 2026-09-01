"""Startup, health and shutdown."""

from __future__ import annotations

from collections.abc import AsyncIterator

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc

from readie_router.app import App, Deps
from readie_router.config import Settings
from tests.fakes.worker import FakeWorker
from tests.integration.conftest import Harness


async def test_health_reports_serving(harness: Harness) -> None:
    """docker-compose healthchecks exactly this."""
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{harness.app.port}")
    try:
        stub = health_pb2_grpc.HealthStub(channel)
        response = await stub.Check(health_pb2.HealthCheckRequest())
        assert response.status == health_pb2.HealthCheckResponse.SERVING
    finally:
        await channel.close()


async def test_reflection_advertises_the_bare_service_names(harness: Harness) -> None:
    """The protos declare no package; the worker and grpcurl depend on that."""
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{harness.app.port}")
    try:
        stub = reflection_pb2_grpc.ServerReflectionStub(channel)

        async def request_stream() -> AsyncIterator[reflection_pb2.ServerReflectionRequest]:
            yield reflection_pb2.ServerReflectionRequest(list_services="")

        names: list[str] = []
        async for response in stub.ServerReflectionInfo(request_stream()):
            names = [s.name for s in response.list_services_response.service]
            break
    finally:
        await channel.close()

    assert "ProxyService" in names
    assert "RegistryService" in names
    assert "grpc.health.v1.Health" in names


async def test_health_goes_not_serving_before_the_drain() -> None:
    """A load balancer must stop sending work before in-flight calls finish."""
    settings = Settings(
        bind_host="127.0.0.1",
        port=0,
        probe_interval=3600.0,
        reaper_interval=3600.0,
        shutdown_grace=1.0,
        log_level="error",
        log_format="console",
    )
    app = App(settings, Deps(executions=FakeWorker()))
    await app.start()

    channel = grpc.aio.insecure_channel(f"127.0.0.1:{app.port}")
    stub = health_pb2_grpc.HealthStub(channel)
    assert (await stub.Check(health_pb2.HealthCheckRequest())).status == (
        health_pb2.HealthCheckResponse.SERVING
    )

    await app.stop()
    await channel.close()


async def test_stop_is_safe_without_a_start() -> None:
    """Startup can fail partway; teardown must not compound it."""
    settings = Settings(
        bind_host="127.0.0.1",
        port=0,
        shutdown_grace=0.5,
        log_level="error",
        log_format="console",
    )
    app = App(settings, Deps(executions=FakeWorker()))
    await app.stop()
