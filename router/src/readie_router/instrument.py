"""Structured logging.

The previous implementation used ``print`` - one line per response chunk, no
level, no correlation. Every log line here carries the identifiers needed to
follow one request across the router, the worker and the client, using the same
key names the Go worker uses so the two services' logs join.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from structlog.stdlib import BoundLogger

# Attribute keys, shared with the Go worker's internal/logging package. Constants
# rather than literals so the "request_id" / "requestId" / "request-id" drift
# that makes logs unqueryable cannot start.
KEY_REQUEST_ID = "request_id"
KEY_SESSION_ID = "session_id"
KEY_CONTAINER_ID = "container_id"
KEY_WORKER_ID = "worker_id"
KEY_CHECKPOINT_ID = "checkpoint_id"
KEY_ERROR = "err"

_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def setup_tracer(endpoint: str, service_name: str) -> TracerProvider:
    """Set up global tracer provider.

    Returns the global `TracerProvider`, which should be explicitly shut down at
    program exit so no traces are lost.
    """
    tracer = TracerProvider(resource=Resource.create(attributes={SERVICE_NAME: service_name}))
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    tracer.add_span_processor(processor)
    trace.set_tracer_provider(tracer)
    return tracer


def configure_logging(
    *,
    level: str = "info",
    fmt: Literal["json", "console"] = "json",
) -> None:
    """Install the logging configuration for the process.

    Safe to call more than once; the last call wins. Tests call it with
    ``console`` to get readable output.
    """
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(_LEVELS[level]),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # grpc and asyncio log through stdlib logging; route them at the same level
    # so a warning from the transport is not silently dropped.
    logging.basicConfig(level=_LEVELS[level], format="%(message)s", force=True)


def get_logger(name: str, **initial: Any) -> BoundLogger:
    """Return a logger bound to a component name and any initial context."""
    logger: BoundLogger = structlog.get_logger(name).bind(**initial)
    return logger


def trace_exc(exc: BaseException, span: trace.Span | None, msg: str | None = None) -> None:
    """Record an exception in the current span if it is active."""
    span = span or trace.get_current_span()
    if span.is_recording():
        span.record_exception(exc)
        span.set_status(trace.StatusCode.ERROR, description=f"{msg}: {exc}" if msg else str(exc))
