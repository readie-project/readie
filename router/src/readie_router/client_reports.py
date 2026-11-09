"""Bounded, process-local capabilities for untrusted client timing reports."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

import grpc
from opentelemetry import trace

from readie_router.clock import Clock
from readie_router.proto import proxy_pb2

METADATA_KEY = "readie-client-report-token"
MAX_REPORT_BYTES = 1024
TOKEN_LENGTH = 43
MAX_CORRELATION_LENGTH = 256
MAX_TIMESTAMP = (1 << 63) - 1


class ReportError(Exception):
    """A rejected report, classified without retaining its payload."""

    def __init__(self, status: grpc.StatusCode) -> None:
        super().__init__(status.name)
        self.status = status


@dataclass(slots=True)
class _Grant:
    parent: trace.SpanContext
    execution_id: str
    request_id: str
    session_id: str
    expires: float
    submission: bytes | None = None


class ClientReports:
    """Issues capabilities and exports at most one child per logical report.

    All methods run without awaiting on the router event loop. Acknowledgement
    means handed to the tracer, not durably delivered to the backend.
    """

    def __init__(
        self,
        clock: Clock,
        tracer: trace.Tracer,
        *,
        ttl: float,
        active_ttl: float,
        capacity: int,
        max_duration: float,
    ) -> None:
        self._clock = clock
        self._tracer = tracer
        self._ttl = ttl
        self._active_ttl = active_ttl
        self._capacity = capacity
        self._max_duration_ns = int(max_duration * 1_000_000_000)
        self._grants: dict[bytes, _Grant] = {}

    @contextmanager
    def grant_report_token(
        self, span: trace.Span, header: proxy_pb2.ClientExecutionRequest
    ) -> Generator[str]:
        """Generate a client report token, associating it with the current parent span.

        After the router receives the request, the client has `self._active_ttl`
        seconds before the token expires. After the router sends its response,
        the client has `self._ttl` seconds before the token expires.
        """
        token = ""
        if header.config.client_reports:
            self.sweep()

        parent = span.get_span_context()
        if (
            header.config.client_reports
            and span.is_recording()
            and parent.is_valid
            and parent.trace_flags.sampled
            and len(self._grants) < self._capacity
        ):
            token = secrets.token_urlsafe(32)
            key = _token_key(token)
            execution_id = uuid.uuid4().hex
            span.set_attribute("readie.execution_id", execution_id)
            self._grants[key] = _Grant(
                parent=parent,
                execution_id=execution_id,
                request_id=header.request_id[:MAX_CORRELATION_LENGTH],
                session_id=header.session_id[:MAX_CORRELATION_LENGTH],
                expires=self._clock.now() + self._active_ttl,
            )
        try:
            yield token
        finally:
            if token:
                grant = self._grants.get(_token_key(token))
                if grant is not None:
                    grant.expires = min(grant.expires, self._clock.now() + self._ttl)

    def sweep(self) -> None:
        """Remove expired grants."""
        now = self._clock.now()
        # Can't use an iterator here because it will break as we delete keys
        for key in [key for key, grant in self._grants.items() if grant.expires <= now]:
            del self._grants[key]

    def submit(self, report: proxy_pb2.SubmitClientReportRequest) -> None:
        """Validate and synchronously accept a report or an identical retry."""
        if report.ByteSize() > MAX_REPORT_BYTES:
            raise ReportError(grpc.StatusCode.RESOURCE_EXHAUSTED)
        if len(report.token) != TOKEN_LENGTH or any(
            ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for ch in report.token
        ):
            raise ReportError(grpc.StatusCode.PERMISSION_DENIED)

        grant = self._grants.get(_token_key(report.token))
        if grant is None or grant.expires <= self._clock.now():
            raise ReportError(grpc.StatusCode.PERMISSION_DENIED)

        self._validate(report)
        # Construct a new message so hidden fields / state don't interfere with
        # report identification
        normalized_report = proxy_pb2.SubmitClientReportRequest(
            start_time_unix_ns=report.start_time_unix_ns,
            end_time_unix_ns=report.end_time_unix_ns,
            duration_ns=report.duration_ns,
            outcome=report.outcome,
            error=report.error,
        )
        request_hash = hashlib.sha256(
            normalized_report.SerializeToString(deterministic=True)
        ).digest()
        if grant.submission is not None:
            if request_hash != grant.submission:
                raise ReportError(grpc.StatusCode.ALREADY_EXISTS)
            return

        context = trace.set_span_in_context(trace.NonRecordingSpan(grant.parent))
        span = self._tracer.start_span(
            "client.reported_e2e",
            context=context,
            kind=trace.SpanKind.INTERNAL,
            start_time=report.start_time_unix_ns,
            attributes={
                "readie.telemetry.source": "client_report",
                "readie.execution_id": grant.execution_id,
                "request_id": grant.request_id,
                "session_id": grant.session_id,
                "readie.client.end_time_unix_ns": report.end_time_unix_ns,
                "readie.client.duration_ns": report.duration_ns,
                "readie.client.outcome": proxy_pb2.ClientReportOutcome.Name(report.outcome),
            },
        )

        # CLIENT_REPORT_ERROR_UNKNOWN means no error so we don't have to set an error
        if report.error != proxy_pb2.CLIENT_REPORT_ERROR_UNKNOWN:
            span.set_attribute(
                "readie.client.error", proxy_pb2.ClientReportError.Name(report.error)
            )
        if report.outcome != proxy_pb2.CLIENT_REPORT_OUTCOME_SUCCESS:
            span.set_status(trace.StatusCode.ERROR)

        span.end(end_time=report.start_time_unix_ns + report.duration_ns)
        grant.submission = request_hash

    def _validate(self, report: proxy_pb2.SubmitClientReportRequest) -> None:
        if (
            report.start_time_unix_ns <= 0
            or report.end_time_unix_ns <= 0
            or report.duration_ns > self._max_duration_ns
            or report.start_time_unix_ns + report.duration_ns > MAX_TIMESTAMP
        ):
            raise ReportError(grpc.StatusCode.INVALID_ARGUMENT)

        if report.outcome not in proxy_pb2.ClientReportOutcome.values():
            raise ReportError(grpc.StatusCode.INVALID_ARGUMENT)
        if report.error not in proxy_pb2.ClientReportError.values():
            raise ReportError(grpc.StatusCode.INVALID_ARGUMENT)

        no_error = report.error == proxy_pb2.CLIENT_REPORT_ERROR_UNKNOWN
        if no_error == (report.outcome == proxy_pb2.CLIENT_REPORT_OUTCOME_ERROR):
            raise ReportError(grpc.StatusCode.INVALID_ARGUMENT)


def _token_key(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()
