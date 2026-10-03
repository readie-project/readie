"""Typed configuration, loaded once from the environment.

Settings are validated at startup and then immutable. The previous
implementation read ``os.environ`` at the point of use and interpolated the
results straight into a listen address, so a missing variable surfaced as a
malformed bind rather than as a configuration error.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Router configuration.

    Every field is populated from an environment variable of the same name in
    upper case. Defaults are chosen so that the only variables a deployment
    must set are the ones that differ between environments.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # -- Identity and transport ------------------------------------------
    service_name: str = "router"
    #: 0 asks the OS for an ephemeral port, which is how tests bind without
    #: racing each other for a fixed one.
    port: int = Field(default=50051, ge=0, le=65535)

    #: The address the gRPC server binds.
    #:
    #: Deliberately distinct from the advertised name. The previous
    #: implementation bound ``f"{SERVICE_NAME}:{PORT}"``, i.e. the hostname,
    #: which happens to work inside a compose network and fails anywhere the
    #: name does not resolve to a local interface.
    bind_host: str = "0.0.0.0"  # noqa: S104 - a server binding all interfaces is the point

    max_concurrent_rpcs: int | None = None
    max_message_bytes: int = Field(default=16 * 1024 * 1024, gt=0)

    # -- Placement --------------------------------------------------------
    #: Fraction of a worker's memory the router is willing to commit.
    memory_headroom: float = Field(default=0.9, gt=0.0, le=1.0)

    #: Memory budget, in bytes, given to a request that does not set one on its
    #: ``@remote`` decorator. The worker maps it onto a container memory limit
    #: and auto-expands from there when the container needs more.
    default_memory: int = Field(default=1024 * 1024 * 1024, gt=0)

    #: Directory of per-flavor ``<flavor>.json`` checkpoint catalogues (mounted
    #: from the generations). Empty or absent means no request-time selection -
    #: every cold start is uncheckpointed, the behaviour before catalogues.
    catalogue_dir: str = ""

    # -- Session serialisation -------------------------------------------
    #: How long a request will wait for its session's turn.
    #:
    #: A session maps to one idle container holding one Python process behind
    #: one socket, so concurrent requests for a session must be serialised.
    #: This bounds the resulting queue.
    session_wait_timeout: float = Field(default=60.0, gt=0)

    # -- Execution --------------------------------------------------------
    execution_timeout: float = Field(default=3600.0, gt=0)

    # -- Liveness and eviction -------------------------------------------
    #: An idle worker sends nothing, so liveness is probed rather than
    #: inferred from the last message received.
    probe_interval: float = Field(default=5.0, gt=0)
    probe_timeout: float = Field(default=2.0, gt=0)
    probe_failure_threshold: int = Field(default=3, ge=1)

    reaper_interval: float = Field(default=5.0, gt=0)
    worker_ttl: float = Field(default=30.0, gt=0)
    executor_ttl: float = Field(default=600.0, gt=0)
    executor_error_ttl: float = Field(default=60.0, gt=0)

    # -- Security ---------------------------------------------------------
    # All opt-in: unset means plaintext with no auth, the documented default.
    #: When set, ProxyService (the only path that runs code) requires this
    #: bearer token in the ``authorization`` metadata; workers, health and
    #: reflection stay exempt. See SECURITY.md.
    auth_token: str = ""

    # -- Lifecycle --------------------------------------------------------
    shutdown_grace: float = Field(default=25.0, gt=0)

    # -- Observability ----------------------------------------------------
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: Literal["json", "console"] = "json"

    @field_validator("log_level", "log_format", mode="before")
    @classmethod
    def _lowercase(cls, value: object) -> object:
        """Accept LOG_LEVEL=INFO as readily as LOG_LEVEL=info."""
        return value.lower() if isinstance(value, str) else value

    @property
    def listen_addr(self) -> str:
        """The address to bind."""
        return f"{self.bind_host}:{self.port}"

    @property
    def advertised_addr(self) -> str:
        """How peers reach this router. Distinct from what it binds."""
        return f"{self.service_name}:{self.port}"

    @property
    def lease_ttl(self) -> float:
        """How long a placement may stay open before it is force-released.

        Twice the execution timeout: this only ever fires when a handler's
        ``finally`` somehow did not run, so it must not race a slow execution.
        """
        return self.execution_timeout * 2
