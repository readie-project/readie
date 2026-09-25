"""Harness settings.

One validated object built from the environment, rather than ``os.environ[...]``
scattered through modules. The agent talks to Claude on Azure AI Foundry via the
``anthropic`` SDK's ``AnthropicFoundry`` client, reading the same ``AZURE_*`` variables
the pipeline uses; a clear error names any that are missing when the agent runs.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from readie_evals.catalogue import default_catalogue_path

#: Where the local stack's nginx listens after ``make run-local``.
DEFAULT_ROUTER_URI = "localhost:50051"

#: The output directory, relative to the component root (``evals/``).
_DEFAULT_OUT_DIR = Path(__file__).resolve().parents[2] / "out"


class ConfigError(Exception):
    """The harness was configured with something unusable."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the harness needs to know."""

    router_uri: str = DEFAULT_ROUTER_URI
    """host:port of the Readie router (through nginx). Local stack: localhost:50051."""

    tls: bool = False
    """Whether the client connects over TLS. Off for the local plaintext stack."""

    call_timeout: float | None = 300.0
    """Per-call deadline, in seconds. ``None`` means no deadline.

    120s once looked generous but was too tight for a real task pulling in a
    heavy, not-yet-cached package (e.g. ``sentence_transformers``, which drags in
    torch): the install alone can eat most of that before the function even
    starts, and the client's own deadline then fires as ``RemoteTimeoutError``
    on a task that was simply still installing, not stuck.
    """

    azure_api_key: str = ""
    """Azure AI Foundry API key for the Anthropic deployment."""

    azure_endpoint: str = ""
    """The Foundry Anthropic endpoint URL (passed to AnthropicFoundry as base_url)."""

    azure_model: str = ""
    """The Claude model / deployment name the agent calls."""

    default_memory: str = "2Gi"
    """The ``@remote`` memory budget a task runs under unless it declares its own."""

    per_cell: int = 3
    """How many tasks to target per (source, category). ``fetch`` fills the deficit."""

    concurrency: int = 10
    """How many containers ``run --target real`` may keep in flight at once.

    Bounds both how many tasks run concurrently and, within one task, its
    checkpoint and cold-start sides running against each other -- the two are
    the same pool (see ``Executor``'s docstring), so this is a cap on total
    concurrent containers, not on tasks times two. 1 falls back to the
    original strictly sequential behaviour. Ignored on ``local``, which never
    touches a container.
    """

    container_release_wait: float = 35.0
    """Seconds to hold a container pool slot after a session finishes, before
    freeing it for the next container.

    A finished call's container does not vanish the instant the response
    comes back -- it goes idle first, for ``SANDBOX_IDLE_TTL`` (worker
    default: 30s), before the worker even starts reclaiming it. Freeing this
    slot immediately would let a new container start booting on the same
    host while the old one is still just sitting there idle, adding load
    instead of handing any back -- exactly what drove the host into the CPU
    contention that made ``executor.ErrDialTimeout`` (surfaced to a client as
    ``ClusterUnavailableError``) far more frequent once concurrency went up.
    Set above the worker's idle TTL so the reaper has already had a chance to
    act on the old container before this slot is reused.
    """

    out_dir: Path = field(default_factory=lambda: _DEFAULT_OUT_DIR)
    catalogue_path: Path = field(default_factory=default_catalogue_path)

    @property
    def corpus_path(self) -> Path:
        """The append-only corpus of fetched/adapted tasks."""
        return self.out_dir / "corpus.jsonl"

    @property
    def results_path(self) -> Path:
        """The JSON Lines results file."""
        return self.out_dir / "results.jsonl"

    @property
    def reports_dir(self) -> Path:
        """Where ``report`` writes its CSV and Markdown."""
        return self.out_dir / "reports"

    @property
    def agent_configured(self) -> bool:
        """Whether the Foundry credentials the agent needs are all present."""
        return bool(self.azure_api_key and self.azure_endpoint and self.azure_model)

    def require_agent(self) -> None:
        """Fail clearly, naming any missing Foundry variables the agent needs."""
        missing = [
            name
            for name, value in (
                ("AZURE_ENDPOINT", self.azure_endpoint),
                ("AZURE_API_KEY", self.azure_api_key),
                ("AZURE_MODEL_NAME", self.azure_model),
            )
            if not value
        ]
        if missing:
            msg = f"the agent needs {', '.join(missing)}; set them or use a .env file"
            raise ConfigError(msg)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read settings, applying defaults for everything optional."""
        source = os.environ if env is None else env
        out = source.get("READIE_EVALS_OUT_DIR")
        return cls(
            router_uri=source.get("READIE_ROUTER_URI") or DEFAULT_ROUTER_URI,
            tls=_flag(source.get("READIE_TLS"), default=False),
            call_timeout=_optional_float(source.get("READIE_EVALS_TIMEOUT"), default=300.0),
            azure_api_key=(source.get("AZURE_API_KEY") or "").strip(),
            azure_endpoint=(source.get("AZURE_ENDPOINT") or "").strip(),
            azure_model=(source.get("AZURE_MODEL_NAME") or "").strip(),
            default_memory=source.get("READIE_EVALS_MEMORY") or "2Gi",
            per_cell=int(source.get("READIE_EVALS_PER_CELL") or "3"),
            concurrency=int(source.get("READIE_EVALS_CONCURRENCY") or "10"),
            container_release_wait=float(source.get("READIE_EVALS_RELEASE_WAIT") or "35.0"),
            out_dir=Path(out) if out else _DEFAULT_OUT_DIR,
        )


def _flag(raw: str | None, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _optional_float(raw: str | None, *, default: float | None) -> float | None:
    if raw is None or not raw.strip():
        return default
    text = raw.strip().lower()
    if text in {"none", "off", "0"}:
        return None
    try:
        return float(text)
    except ValueError as exc:
        msg = f"timeout must be a number or none, got {raw!r}"
        raise ConfigError(msg) from exc
