"""Real gRPC capability issuance, authentication, and synthetic child export."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import grpc
import pytest
import pytest_asyncio
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from readie_router.app import App, Deps
from readie_router.client_reports import METADATA_KEY
from readie_router.config import Settings
from readie_router.proto import proxy_pb2 as pb
from readie_router.proto import proxy_pb2_grpc, registry_pb2, registry_pb2_grpc
from tests.fakes.worker import FakeWorker
from tests.integration.test_proxy_service import stream_of


@dataclass
class ReportRouter:
    proxy: proxy_pb2_grpc.ProxyServiceStub
    exporter: InMemorySpanExporter


@pytest_asyncio.fixture
async def report_router(settings: Settings) -> AsyncIterator[ReportRouter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    worker = FakeWorker()
    app = App(
        settings.model_copy(update={"auth_token": "secret"}),
        Deps(executions=worker, tracer=provider.get_tracer("test")),
    )
    await app.start()
    async with grpc.aio.insecure_channel(f"127.0.0.1:{app.port}") as channel:
        await registry_pb2_grpc.RegistryServiceStub(channel).PostWorkerStatus(
            registry_pb2.WorkerStatus(
                worker_id="worker", worker_uri="worker:50052", status=registry_pb2.STATUS_READY
            )
        )
        try:
            yield ReportRouter(proxy_pb2_grpc.ProxyServiceStub(channel), exporter)
        finally:
            await app.stop()
            provider.shutdown()


def execution(
    router: ReportRouter,
) -> grpc.aio.StreamStreamCall[pb.ClientExecutionRequest, pb.ClientExecutionResponse]:
    return router.proxy.RequestExecution(
        stream_of(
            pb.ClientExecutionRequest(
                request_id="request",
                config=pb.ExecutionConfig(client_reports=True),
            )
        ),
        metadata=(
            ("authorization", "Bearer secret"),
            ("traceparent", "00-" + "1" * 32 + "-" + "2" * 16 + "-00"),
            ("baggage", "private=value"),
        ),
    )


def completion(token: str) -> pb.SubmitClientReportRequest:
    return pb.SubmitClientReportRequest(
        token=token,
        start_time_unix_ns=1000,
        end_time_unix_ns=2000,
        duration_ns=1000,
        outcome=pb.CLIENT_REPORT_OUTCOME_SUCCESS,
    )


async def test_public_context_is_ignored_and_concurrent_retries_export_one_child(
    report_router: ReportRouter,
) -> None:
    call = execution(report_router)
    token = dict(await call.initial_metadata())[METADATA_KEY]
    assert [item async for item in call]
    await asyncio.gather(
        *[
            report_router.proxy.SubmitClientReport(
                completion(token), metadata=(("authorization", "Bearer secret"),)
            )
            for _ in range(5)
        ]
    )
    spans = report_router.exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "ProxyService/RequestExecution")
    children = [s for s in spans if s.name == "client.reported_e2e"]
    assert root.parent is None
    assert root.context is not None
    assert root.context.trace_id != int("1" * 32, 16)
    assert len(children) == 1
    assert children[0].parent == root.context


async def test_report_authentication_has_unary_cardinality_and_requires_both_tokens(
    report_router: ReportRouter,
) -> None:
    call = execution(report_router)
    token = dict(await call.initial_metadata())[METADATA_KEY]
    _ = [item async for item in call]
    with pytest.raises(grpc.aio.AioRpcError) as raised:
        await report_router.proxy.SubmitClientReport(completion(token))
    assert raised.value.code() == grpc.StatusCode.UNAUTHENTICATED
    with pytest.raises(grpc.aio.AioRpcError) as raised:
        await report_router.proxy.SubmitClientReport(
            completion("x" * 43), metadata=(("authorization", "Bearer secret"),)
        )
    assert raised.value.code() == grpc.StatusCode.PERMISSION_DENIED
    await report_router.proxy.SubmitClientReport(
        completion(token), metadata=(("authorization", "Bearer secret"),)
    )
