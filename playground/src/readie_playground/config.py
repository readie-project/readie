"""Service settings, read from ``PLAYGROUND_*`` environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

# The playground's resource budget is fixed, not user-selectable: every run asks for
# the same initial and maximum memory, so a visitor cannot request more.
MEMORY = "4Gi"


@dataclass(frozen=True, slots=True)
class PlaygroundSettings:
    """Everything the service reads from its environment."""

    host: str = "0.0.0.0"  # noqa: S104 - runs in a container behind a proxy
    port: int = 8080
    router_uri: str = ""  # empty: the address the Readie client uses by default
    allowed_origins: tuple[str, ...] = ("https://readie.org",)
    max_code_bytes: int = 16 * 1024
    max_output_chars: int = 64 * 1024
    timeout_s: float = 30.0
    rate_limit_requests: int = 10
    rate_limit_window_s: float = 60.0
    max_concurrent_runs: int = 4
    trust_proxy: bool = False
    router_tls: bool = True
    workdir: str = "/tmp/playground"  # noqa: S108 - a tmpfs inside the container

    @classmethod
    def from_env(cls) -> PlaygroundSettings:
        """Build settings from ``PLAYGROUND_*`` variables, falling back to defaults."""
        d = cls()
        env = os.environ.get
        origins = env("PLAYGROUND_ALLOWED_ORIGINS")
        return cls(
            host=env("PLAYGROUND_HOST", d.host),
            port=int(env("PLAYGROUND_PORT", d.port)),
            router_uri=env("PLAYGROUND_ROUTER_URI", d.router_uri),
            allowed_origins=(
                tuple(o.strip() for o in origins.split(",") if o.strip())
                if origins
                else d.allowed_origins
            ),
            max_code_bytes=int(env("PLAYGROUND_MAX_CODE_BYTES", d.max_code_bytes)),
            max_output_chars=int(env("PLAYGROUND_MAX_OUTPUT_CHARS", d.max_output_chars)),
            timeout_s=float(env("PLAYGROUND_TIMEOUT_S", d.timeout_s)),
            rate_limit_requests=int(env("PLAYGROUND_RATE_LIMIT_REQUESTS", d.rate_limit_requests)),
            rate_limit_window_s=float(env("PLAYGROUND_RATE_LIMIT_WINDOW_S", d.rate_limit_window_s)),
            max_concurrent_runs=int(env("PLAYGROUND_MAX_CONCURRENT_RUNS", d.max_concurrent_runs)),
            trust_proxy=env("PLAYGROUND_TRUST_PROXY", "").lower() in {"1", "true", "yes"},
            router_tls=env("PLAYGROUND_ROUTER_TLS", "true").lower() in {"1", "true", "yes"},
            workdir=env("PLAYGROUND_WORKDIR", d.workdir),
        )
