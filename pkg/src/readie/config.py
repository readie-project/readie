"""Client settings.

A frozen dataclass rather than pydantic: this package is imported into a user's
process, and an SDK that drags in a validation framework for six fields is a
dependency the user did not ask for. Validation is explicit in ``__post_init__``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from readie.errors import ConfigurationError

# Public endpoint of the nginx client-facing proxy
DEFAULT_ROUTER_URI = "localhost:50051"
DEFAULT_TIMEOUT = None
DEFAULT_STREAM_LOGS = True
CHUNK_SIZE = 1024 * 1024
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """How the client reaches the router and frames what it sends."""

    router_uri: str = DEFAULT_ROUTER_URI
    """host:port of the router's ProxyService."""

    timeout: float | None = DEFAULT_TIMEOUT
    """Deadline for a whole call, in seconds. ``None`` means no deadline."""

    chunk_size: int = field(default=CHUNK_SIZE, init=False)
    """Payload bytes per stream message. Matches the executor's socket reads."""

    max_message_bytes: int = field(default=MAX_MESSAGE_BYTES, init=False)
    """gRPC message cap. Must exceed ``chunk_size`` with room for the envelope."""

    stream_logs: bool = DEFAULT_STREAM_LOGS
    """Print executor output as it arrives, instead of only on failure."""

    auth_token: str = field(default_factory=lambda: os.environ.get(
        "READIE_AUTH_TOKEN") or "", init=False)
    """Bearer token sent to a router that requires one. Empty sends none."""

    tls: bool = field(default_factory=lambda: _bool(
        "READIE_TLS", default=False), init=False)
    """Connect over TLS. Implied by ``tls_ca``. Off means plaintext."""

    tls_ca: str = field(default_factory=lambda: os.environ.get(
        "READIE_TLS_CA") or "", init=False)
    """Path to a PEM CA bundle that verifies the router. Empty uses TLS with the
    system roots (when ``tls`` is on)."""

    @property
    def use_tls(self) -> bool:
        """Whether to open a TLS channel to the router."""
        return self.tls or bool(self.tls_ca)

    def __post_init__(self) -> None:
        """Reject settings that would fail later, and more confusingly."""
        if not self.router_uri:
            msg = "router_uri is required; set READIE_ROUTER_URI or pass it explicitly"
            raise ConfigurationError(msg)
        if self.chunk_size <= 0:
            msg = f"chunk_size must be positive, got {self.chunk_size}"
            raise ConfigurationError(msg)
        if self.max_message_bytes <= self.chunk_size:
            msg = (
                f"max_message_bytes ({self.max_message_bytes}) must exceed "
                f"chunk_size ({self.chunk_size})"
            )
            raise ConfigurationError(msg)
        if self.timeout is not None and self.timeout <= 0:
            msg = f"timeout must be positive or None, got {self.timeout}"
            raise ConfigurationError(msg)
        if self.stream_logs is not None and not isinstance(self.stream_logs, bool):
            msg = f"stream_logs must be a boolean, got {self.stream_logs}"
            raise ConfigurationError(msg)
