"""Client settings.

A frozen dataclass rather than pydantic: this package is imported into a user's
process, and an SDK that drags in a validation framework for six fields is a
dependency the user did not ask for. Validation is explicit in ``__post_init__``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from crfs.errors import ConfigurationError

DEFAULT_ROUTER_URI = "localhost:50051"
DEFAULT_CHUNK_SIZE = 1024 * 1024
DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Settings:
    """How the client reaches the router and frames what it sends."""

    router_uri: str = DEFAULT_ROUTER_URI
    """host:port of the router's ProxyService."""

    timeout: float | None = None
    """Deadline for a whole call, in seconds. ``None`` means no deadline."""

    chunk_size: int = DEFAULT_CHUNK_SIZE
    """Payload bytes per stream message. Matches the executor's socket reads."""

    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    """gRPC message cap. Must exceed ``chunk_size`` with room for the envelope."""

    stream_logs: bool = True
    """Print executor output as it arrives, instead of only on failure."""

    def __post_init__(self) -> None:
        """Reject settings that would fail later, and more confusingly."""
        if not self.router_uri:
            msg = "router_uri is required; set CRFS_ROUTER_URI or pass it explicitly"
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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from ``CRFS_*`` variables, falling back to defaults."""
        source = os.environ if env is None else env

        def _float(name: str, default: float | None) -> float | None:
            raw = source.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                return float(raw)
            except ValueError as exc:
                msg = f"{name} must be a number, got {raw!r}"
                raise ConfigurationError(msg) from exc

        def _int(name: str, default: int) -> int:
            raw = source.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                return int(raw)
            except ValueError as exc:
                msg = f"{name} must be an integer, got {raw!r}"
                raise ConfigurationError(msg) from exc

        def _bool(name: str, default: bool) -> bool:
            raw = source.get(name)
            if raw is None or not raw.strip():
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        # ROUTER_URI without the prefix is what the old client read; accepting it
        # keeps existing deployments working.
        uri = source.get("CRFS_ROUTER_URI") or source.get("ROUTER_URI") or DEFAULT_ROUTER_URI
        return cls(
            router_uri=uri,
            timeout=_float("CRFS_TIMEOUT", None),
            chunk_size=_int("CRFS_CHUNK_SIZE", DEFAULT_CHUNK_SIZE),
            max_message_bytes=_int("CRFS_MAX_MESSAGE_BYTES", DEFAULT_MAX_MESSAGE_BYTES),
            stream_logs=_bool("CRFS_STREAM_LOGS", default=True),
        )
