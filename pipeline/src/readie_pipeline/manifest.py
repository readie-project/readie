"""The artifact manifest and per-checkpoint metadata.

One writer each, so the shape the worker reads is defined in exactly one place.
The Go reader is ``worker/internal/artifact``; the field names here are the
contract with it.

The output is exactly what the worker image bakes in, so nothing is reshaped
between capturing a checkpoint and shipping it::

    <output>/
    ├── manifest.json
    └── checkpoints/<id>/{meta.json, …runsc image files}

The rootfs is not here: there is one, it comes from the rootfs image, and the
image build copies it in alongside this. That is also why neither file carries a
``rootfs_id`` any more -- with a single rootfs baked in beside the checkpoints
there is nothing to identify, and the pairing is correct by construction rather
than by a field nobody compared.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from readie_pipeline.config import EXECUTOR_ARGV, EXECUTOR_PROTOCOL

#: Written atomically: a manifest half-written by an interrupted build is worse
#: than none, because the worker would load it and refuse the whole generation
#: for a reason that has nothing to do with the checkpoints.
_TEMP_SUFFIX = ".partial"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + _TEMP_SUFFIX)
    temp.write_text(json.dumps(payload, indent=2) + "\n")
    temp.replace(path)


def utc_now() -> str:
    """An RFC 3339 timestamp in UTC, which is what the Go side parses."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True, slots=True)
class Manifest:
    """What the captured checkpoints can be restored into.

    A checkpoint that travels without this cannot be verified before a restore
    is attempted, and gVisor's failure at that point is opaque.

    It carries no identity of its own: the worker image tag names the build.
    """

    runsc_version: str
    spec_fingerprint: str
    python_path: str
    overlay: str
    network: str
    root_readonly: bool = False
    executor_argv: tuple[str, ...] = field(default_factory=lambda: EXECUTOR_ARGV)
    executor_protocol: int = EXECUTOR_PROTOCOL
    created_at: str = ""

    def to_json(self) -> dict[str, Any]:
        """Render to the shape worker/internal/artifact reads."""
        return {
            "runsc_version": self.runsc_version,
            "spec_fingerprint": self.spec_fingerprint,
            # The command, not a path: the executor is an installed wheel, so
            # there is no stable source file to point at. The worker replays
            # this verbatim rather than assembling its own.
            "executor_argv": list(self.executor_argv),
            # The wire format the rootfs's executor speaks. The worker refuses
            # artifacts whose version it does not implement.
            "executor_protocol": self.executor_protocol,
            "python_path": self.python_path,
            "overlay": self.overlay,
            "network": self.network,
            "root_readonly": self.root_readonly,
            "created_at": self.created_at or utc_now(),
        }

    def write(self, directory: Path) -> Path:
        """Write ``manifest.json`` and return its path."""
        path = directory / "manifest.json"
        _write_json(path, self.to_json())
        return path


@dataclass(frozen=True, slots=True)
class CheckpointMeta:
    """What one checkpoint can be restored into.

    The worker reads this before attempting a restore, so a mismatch becomes a
    named, logged downgrade rather than an opaque failure minutes into a request.
    Duplicated from the manifest on purpose: a checkpoint directory should be
    self-describing even in isolation.
    """

    checkpoint_id: str
    runsc_version: str
    spec_fingerprint: str
    imports: tuple[str, ...] = field(default_factory=tuple)
    datasets: tuple[str, ...] = field(default_factory=tuple)
    models: tuple[str, ...] = field(default_factory=tuple)
    tokenizers: tuple[str, ...] = field(default_factory=tuple)
    producer: str = "pipeline"
    created_at: str = ""

    def to_json(self) -> dict[str, Any]:
        """Render to the shape worker/internal/artifact reads."""
        return {
            "checkpoint_id": self.checkpoint_id,
            "producer": self.producer,
            "runsc_version": self.runsc_version,
            "spec_fingerprint": self.spec_fingerprint,
            "imports": list(self.imports),
            "datasets": list(self.datasets),
            "models": list(self.models),
            "tokenizers": list(self.tokenizers),
            "created_at": self.created_at or utc_now(),
        }

    def write(self, directory: Path) -> Path:
        """Write ``meta.json`` into a checkpoint directory."""
        path = directory / "meta.json"
        _write_json(path, self.to_json())
        return path


def write_plan(path: Path, plans: list[dict[str, Any]]) -> None:
    """Write the checkpoint plan, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + _TEMP_SUFFIX)
    temp.write_text(json.dumps(plans, indent=2) + "\n")
    temp.replace(path)
