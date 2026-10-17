"""Pipeline settings.

One validated object built once, rather than ``os.environ[...]`` scattered
through modules that run at import time. The previous scripts read eleven
variables across four files and failed with a ``KeyError`` naming the variable
but not its purpose or its default.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Where the executor binds its socket *inside* the sandbox. A contract with the
#: executor and with every checkpoint already captured.
SANDBOX_EXECUTOR_DIR = "/tmp"  # noqa: S108 - a path inside the sandbox, not a host temp file

#: What the sandbox runs. Must stay in step with the rootfs, which installs the
#: executor as a wheel rather than a loose file.
EXECUTOR_ARGV: tuple[str, ...] = ("python", "-u", "-m", "readie_executor")

#: The executor wire format the rootfs speaks, recorded in the manifest. The
#: worker refuses a generation whose version it does not implement. Must match
#: readie_executor.protocol.PROTOCOL_VERSION.
EXECUTOR_PROTOCOL = 2

# Support upto 6 decimal places for `alpha` value
ALPHA_PRECISION = 6


class ConfigError(Exception):
    """The pipeline was configured with something unusable."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the pipeline needs to know."""

    bundle_dir: Path
    """The OCI bundle: config.json plus the rootfs the sandbox runs."""

    output_dir: Path
    """Host-side build outputs. Deliberately distinct from the sandbox's own
    EXECUTOR_DIR (/tmp, where the executor binds its socket) -- conflating the
    two once sent the pre-imported executor source to the wrong place, so every
    checkpoint captured a bare executor."""

    data_dir: Path
    """Corpus and package metadata."""

    plan_dir: Path | None = None
    """Where the plan and the spec fingerprint go, when they should not go with
    the checkpoints. They are inputs to a capture rather than artifacts the
    worker ships, and output_dir is copied into the worker image wholesale, so
    the image sets this to keep them out of it. None means output_dir, which is
    what a local `plan` run wants."""

    rootfs_pythonpath: str = "/usr/local/lib/python3.12/dist-packages"

    # Runtime modes. Part of what a checkpoint is sensitive to, so they must
    # match the worker's configuration or a restore fails on the fingerprint.
    sandbox_network: str = "sandbox"
    sandbox_host_uds: str = "create"
    sandbox_overlay: str = "root:memory"

    ocispec_binary: str = "/usr/local/bin/ocispec"
    runsc_binary: str = "runsc"

    checkpoint_timeout: float = 300.0
    """How long a sandbox may take to complete checkpointing before the build gives up."""

    #: Which generation this run produces: "cpu" or "gpu". Recorded in the
    #: catalogue so the router knows which flavor of worker its checkpoints
    #: belong to; also gates the GPU sandbox env at capture.
    flavor: str = "cpu"

    base_image: str = ""
    """The base image the analysis runs in, recorded in the metadata it writes.

    Empty when unknown.
    """

    planner: str = "greedy"
    max_checkpoints: int = 8
    checkpoint_size_budget_mb: float = 2048.0
    #: Size-vs-time weight (seconds per MB) the greedy planner trades against.
    #: The router selects the checkpoint minimising ``alpha*size + residual_load``,
    #: the same quantity the planner maximises the reduction of when it adds a package.
    alpha: float = 0.005

    @property
    def rootfs_path(self) -> Path:
        """The root filesystem inside the bundle."""
        return self.bundle_dir / "rootfs"

    @property
    def socket_dir(self) -> Path:
        """Host directory bind-mounted at the sandbox's EXECUTOR_DIR.

        Nothing binds a socket during a build -- the process is captured before
        it gets that far -- but the mount must exist in the spec, because the
        mount list is part of what a checkpoint can be restored into.
        """
        return self.output_dir / "socket"

    @property
    def checkpoints_dir(self) -> Path:
        """Where captured checkpoint images are written."""
        return self.output_dir / "checkpoints"

    @property
    def manifest_path(self) -> Path:
        """The manifest the worker image bakes in beside the checkpoints."""
        return self.output_dir / "manifest.json"

    @property
    def catalogue_path(self) -> Path:
        """The catalogue the router reads to select a checkpoint at request time.

        Written beside the manifest so a generation is self-describing; the
        deployment copies it to the router's ``CATALOGUE_DIR`` as
        ``<flavor>.json``.
        """
        return self.output_dir / "catalogue.json"

    @property
    def plan_path(self) -> Path:
        """The checkpoint plan, written by `plan` and read by `build`."""
        return (self.plan_dir or self.output_dir) / "checkpoints.json"

    @property
    def skipped_path(self) -> Path:
        """The distributions that did not install into the rootfs, copied here by `plan`."""
        return (self.plan_dir or self.output_dir) / "rootfs-skipped.txt"

    @property
    def requirements_path(self) -> Path:
        """The distributions the plan needs installed in the rootfs, written by `plan`."""
        return (self.plan_dir or self.output_dir) / "rootfs-requirements.txt"

    @property
    def fingerprint_path(self) -> Path:
        """The spec fingerprint, so `build` need not regenerate the spec."""
        return (self.plan_dir or self.output_dir) / "spec-fingerprint.txt"

    @property
    def corpus_cache_path(self) -> Path:
        """Resource lookups already answered, so a re-run does not repeat them.

        Shared by every flavor. The cache holds what PyPI, the Hugging Face Hub
        and Kaggle said, and none of that depends on the flavor.
        """
        return self.data_dir / "cache" / "verified.json"

    @property
    def corpus_path(self) -> Path:
        """The request corpus.

        Per-flavor: a gpu corpus can lean on GPU-shaped snippets a cpu one
        never would, and the two must not silently overwrite each other.
        """
        return self.data_dir / "datasets" / f"{self.flavor}.json"

    @property
    def metadata_path(self) -> Path:
        """Measured package sizes and import times.

        Per-flavor, for the same reason as ``corpus_path``: a cpu and a gpu
        base image install different packages (and different builds of shared
        ones), so their measurements cannot share a file.
        """
        return self.data_dir / "metadata" / f"{self.flavor}.json"

    @property
    def global_runsc_flags(self) -> list[str]:
        """Flags that precede every runsc subcommand.

        They must match the worker's, because a checkpoint is sensitive to them.
        runsc parses with stdlib flag semantics, which stop at the first
        non-flag argument, so these cannot follow the subcommand.
        """
        return [
            f"--network={self.sandbox_network}",
            f"--host-uds={self.sandbox_host_uds}",
            f"--overlay2={self.sandbox_overlay}",
            "--ignore-cgroups",
        ]

    def __post_init__(self) -> None:
        """Reject settings that would fail later, and less legibly."""
        if self.max_checkpoints <= 0:
            msg = f"max_checkpoints must be positive, got {self.max_checkpoints}"
            raise ConfigError(msg)
        if self.checkpoint_size_budget_mb <= 0:
            msg = (
                f"checkpoint_size_budget_mb must be positive, got {self.checkpoint_size_budget_mb}"
            )
            raise ConfigError(msg)
        if self.checkpoint_timeout <= 0:
            msg = f"checkpoint_timeout must be positive, got {self.checkpoint_timeout}"
            raise ConfigError(msg)
        if self.alpha <= 0:
            msg = f"alpha must be positive, got {self.alpha}"
            raise ConfigError(msg)
        if self.flavor not in ("cpu", "gpu"):
            msg = f"flavor must be 'cpu' or 'gpu', got {self.flavor!r}"
            raise ConfigError(msg)
        if self.sandbox_overlay.startswith("all:"):
            # Would keep the executor's socket in the overlay's upper layer,
            # where the worker cannot see it. Every execution would then fail at
            # dial time, looking exactly like a dead executor.
            msg = f"sandbox_overlay {self.sandbox_overlay!r} would hide the executor socket"
            raise ConfigError(msg)
        if ":self" in self.sandbox_overlay:
            msg = f"sandbox_overlay {self.sandbox_overlay!r} writes into the shared rootfs"
            raise ConfigError(msg)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: object) -> Settings:
        """Build settings from the environment, with explicit overrides winning.

        Overrides come from the command line, so they take precedence over the
        image's baked-in defaults.
        """
        source = os.environ if env is None else env

        def path_of(name: str, default: str) -> Path:
            return Path(source.get(name) or default)

        def optional_path(name: str) -> Path | None:
            raw = (source.get(name) or "").strip()
            return Path(raw) if raw else None

        values: dict[str, object] = {
            "bundle_dir": path_of("BASE_DIR", "/app/executorfs"),
            "output_dir": path_of("EXECUTOR_DIR", "/app/executor"),
            "data_dir": path_of("READIE_DATA_DIR", str(_default_data_dir())),
            "plan_dir": optional_path("READIE_PLAN_DIR"),
            "rootfs_pythonpath": source.get("ROOTFS_PYTHONPATH")
            or "/usr/local/lib/python3.12/dist-packages",
            "sandbox_network": source.get("SANDBOX_NETWORK") or "sandbox",
            "sandbox_host_uds": source.get("SANDBOX_HOST_UDS") or "create",
            "sandbox_overlay": source.get("SANDBOX_OVERLAY") or "root:memory",
            "ocispec_binary": source.get("OCISPEC_BINARY") or "/usr/local/bin/ocispec",
            "runsc_binary": source.get("RUNSC_BINARY") or "runsc",
            "checkpoint_timeout": _float(source, "READIE_CHECKPOINT_TIMEOUT", 300.0),
            "flavor": source.get("FLAVOR") or source.get("READIE_FLAVOR") or "cpu",
            "base_image": source.get("READIE_BASE_IMAGE") or "",
            "planner": source.get("READIE_PLANNER") or "greedy",
            "max_checkpoints": _int(source, "READIE_MAX_CHECKPOINTS", 8),
            "checkpoint_size_budget_mb": _float(source, "READIE_SIZE_BUDGET_MB", 2048.0),
            "alpha": _float(source, "READIE_ALPHA", 0.005, ALPHA_PRECISION),
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)  # type: ignore[arg-type]


def _default_data_dir() -> Path:
    """The corpus, cache and metadata folder, packaged alongside the code."""
    return Path(__file__).resolve().parent.parent.parent / "data"


def _int(source: Mapping[str, str], name: str, default: int) -> int:
    raw = (source.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        msg = f"{name} must be an integer, got {raw!r}"
        raise ConfigError(msg) from exc


def _float(
    source: Mapping[str, str], name: str, default: float, ndigits: int | None = None
) -> float:
    raw = (source.get(name) or "").strip()
    if not raw:
        return default
    try:
        if ndigits is not None and ndigits > 0:
            return round(float(raw), ndigits)
        return float(raw)
    except ValueError as exc:
        msg = f"{name} must be a number, got {raw!r}"
        raise ConfigError(msg) from exc


@dataclass(frozen=True, slots=True)
class CorpusSettings:
    """Credentials and knobs for corpus generation.

    Separate from Settings because generating a corpus is an authoring step that
    needs a hosted model and, for real code, Kaggle and Hugging Face access,
    and building checkpoints needs none of them.
    """

    endpoint: str
    api_key: str
    model: str
    total: int = 2000
    """New items one run appends to the corpus for the flavor."""
    source_weights: str = ""
    """``source=weight`` overrides of the default mix; see ``corpus.mix``."""
    batch_size: int = 10
    seed: int | None = None
    kaggle_token: str | None = None
    hf_token: str | None = None
    rewrite: bool = True
    """Replace reads of missing files with generated stand-in data."""
    rewrite_max_chars: int = 60_000
    """Items with more code than this are not rewritten."""
    rewrite_workers: int = 6
    """Model calls for rewrites that run at the same time."""

    def __post_init__(self) -> None:
        """Reject values that would make a run meaningless."""
        if self.total <= 0:
            msg = f"READIE_CORPUS_TOTAL must be positive, got {self.total}"
            raise ConfigError(msg)
        if self.batch_size <= 0:
            msg = f"READIE_CORPUS_BATCH must be positive, got {self.batch_size}"
            raise ConfigError(msg)
        if self.rewrite_max_chars <= 0 or self.rewrite_workers <= 0:
            msg = (
                "READIE_CORPUS_REWRITE_MAX_CHARS and READIE_CORPUS_REWRITE_WORKERS must be positive"
            )
            raise ConfigError(msg)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CorpusSettings:
        """Read corpus settings, failing clearly when a required one is missing.

        Only the model credentials are required here. Kaggle and Hugging Face
        credentials are required exactly when a source that uses them has a
        non-zero weight, which is checked once the weights are known.
        """
        source = os.environ if env is None else env

        missing = [
            name
            for name in ("AZURE_ENDPOINT", "AZURE_API_KEY", "AZURE_MODEL_NAME")
            if not (source.get(name) or "").strip()
        ]
        if missing:
            msg = f"corpus generation needs {', '.join(missing)}; set them or use a .env file"
            raise ConfigError(msg)

        seed = (source.get("READIE_CORPUS_SEED") or "").strip()
        return cls(
            endpoint=source["AZURE_ENDPOINT"].strip(),
            api_key=source["AZURE_API_KEY"].strip(),
            model=source["AZURE_MODEL_NAME"].strip(),
            total=_int(source, "READIE_CORPUS_TOTAL", 2000),
            source_weights=(source.get("READIE_CORPUS_SOURCE_WEIGHTS") or "").strip(),
            batch_size=_int(source, "READIE_CORPUS_BATCH", 10),
            seed=_int(source, "READIE_CORPUS_SEED", 0) if seed else None,
            kaggle_token=(source.get("KAGGLE_API_TOKEN") or "").strip() or None,
            hf_token=(source.get("HF_TOKEN") or "").strip() or None,
            rewrite=(source.get("READIE_CORPUS_REWRITE") or "1").strip().lower()
            not in {"0", "false", "no", "off"},
            rewrite_max_chars=_int(source, "READIE_CORPUS_REWRITE_MAX_CHARS", 60_000),
            rewrite_workers=_int(source, "READIE_CORPUS_REWRITE_WORKERS", 6),
        )
