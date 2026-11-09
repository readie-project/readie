"""Core report validation, lifetime, and synthetic-span guarantees."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import grpc
import pytest
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from readie_router.client_reports import MAX_TIMESTAMP, ClientReports, ReportError
from readie_router.clock import FakeClock
from readie_router.proto import proxy_pb2 as pb


@dataclass
class Rig:
    reports: ClientReports
    clock: FakeClock
    tracer: trace.Tracer
    exporter: InMemorySpanExporter

    def issue(self) -> tuple[str, trace.SpanContext]:
        header = pb.ClientExecutionRequest(config=pb.ExecutionConfig(client_reports=True))
        with (
            self.tracer.start_as_current_span("router", context=Context()) as parent,
            self.reports.grant_report_token(parent, header) as token,
        ):
            return token, parent.get_span_context()


@pytest.fixture
def rig() -> Iterator[Rig]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    clock = FakeClock()
    tracer = provider.get_tracer("test")
    reports = ClientReports(clock, tracer, ttl=5, active_ttl=20, capacity=2, max_duration=10)
    yield Rig(reports, clock, tracer, exporter)
    provider.shutdown()


def report(token: str) -> pb.SubmitClientReportRequest:
    return pb.SubmitClientReportRequest(
        token=token,
        start_time_unix_ns=1000,
        end_time_unix_ns=900,  # Wall-clock steps must not change elapsed time.
        duration_ns=20,
        outcome=pb.CLIENT_REPORT_OUTCOME_SUCCESS,
    )


@pytest.mark.parametrize("failed", [False, True])
def test_late_reports_use_the_saved_parent_and_monotonic_duration(rig: Rig, failed: bool) -> None:
    token, parent = rig.issue()
    message = report(token)
    if failed:
        message.outcome = pb.CLIENT_REPORT_OUTCOME_ERROR
        message.error = pb.CLIENT_REPORT_ERROR_TIMEOUT
    with rig.tracer.start_as_current_span("unrelated upload", context=Context()):
        rig.reports.submit(message)
    child = next(s for s in rig.exporter.get_finished_spans() if s.name == "client.reported_e2e")
    assert child.context is not None
    assert child.context.trace_id == parent.trace_id
    assert child.context.span_id != parent.span_id
    assert child.parent == parent
    assert (child.start_time, child.end_time) == (1000, 1020)
    assert child.attributes is not None
    assert child.attributes["readie.telemetry.source"] == "client_report"
    assert child.attributes["readie.client.end_time_unix_ns"] == 900
    assert child.attributes.get("readie.client.error") == (
        "CLIENT_REPORT_ERROR_TIMEOUT" if failed else None
    )
    assert child.status.status_code == (
        trace.StatusCode.ERROR if failed else trace.StatusCode.UNSET
    )
    assert not child.events


def test_retries_ignore_unknown_fields_but_reject_conflicting_values(rig: Rig) -> None:
    token, _ = rig.issue()
    message = report(token)
    rig.reports.submit(message)
    # Unknown field 100 must not turn the same logical report into a conflict.
    rig.reports.submit(
        pb.SubmitClientReportRequest.FromString(message.SerializeToString() + b"\xa0\x06\x01")
    )
    assert sum(s.name == "client.reported_e2e" for s in rig.exporter.get_finished_spans()) == 1
    message.duration_ns += 1
    with pytest.raises(ReportError) as raised:
        rig.reports.submit(message)
    assert raised.value.status == grpc.StatusCode.ALREADY_EXISTS


@pytest.mark.parametrize(
    ("field", "value", "status"),
    [
        ("token", "unknown", grpc.StatusCode.PERMISSION_DENIED),
        ("token", "a" * 1025, grpc.StatusCode.RESOURCE_EXHAUSTED),
        ("start_time_unix_ns", MAX_TIMESTAMP, grpc.StatusCode.INVALID_ARGUMENT),
        ("duration_ns", 10_000_000_001, grpc.StatusCode.INVALID_ARGUMENT),
        ("outcome", 99, grpc.StatusCode.INVALID_ARGUMENT),
        ("outcome", pb.CLIENT_REPORT_OUTCOME_ERROR, grpc.StatusCode.INVALID_ARGUMENT),
    ],
)
def test_rejections_leave_the_capability_usable(
    rig: Rig, field: str, value: str | int, status: grpc.StatusCode
) -> None:
    token, _ = rig.issue()
    invalid = report(token)
    setattr(invalid, field, value)
    with pytest.raises(ReportError) as raised:
        rig.reports.submit(invalid)
    assert raised.value.status == status
    rig.reports.submit(report(token))


def test_capacity_counts_accepted_reports_until_expiry(rig: Rig) -> None:
    token, _ = rig.issue()
    rig.reports.submit(report(token))
    assert rig.issue()[0]
    assert rig.issue()[0] == ""
    rig.clock.advance(5)
    with pytest.raises(ReportError) as raised:
        rig.reports.submit(report(token))
    assert raised.value.status == grpc.StatusCode.PERMISSION_DENIED
    assert rig.issue()[0]
