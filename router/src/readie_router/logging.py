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


def configure(
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


def get_logger(name: str, **initial: Any) -> structlog.stdlib.BoundLogger:
    """Return a logger bound to a component name and any initial context."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name).bind(**initial)
    return logger


def discard_logger() -> structlog.stdlib.BoundLogger:
    """Return a logger that writes nothing.

    For tests that assert behaviour rather than output.
    """
    logger: structlog.stdlib.BoundLogger = structlog.wrap_logger(
        None,
        wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL + 1),
        processors=[],
    )
    return logger
