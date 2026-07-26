"""The generation manifest and per-checkpoint metadata.

One writer each, so the shape the worker reads is defined in exactly one place.
The Go reader is ``worker/internal/artifact``; the field names here are the
contract with it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crfs_pipeline.config import EXECUTOR_ARGV, EXECUTOR_PROTOCOL

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
class Generation:
    """What binds a set of checkpoints to the filesystem they were captured on.

    A checkpoint that travels without this cannot be verified before a restore
    is attempted, and gVisor's failure at that point is opaque.
    """

    id: str
    rootfs_id: str
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
            "id": self.id,
            "rootfs_id": self.rootfs_id,
            "runsc_version": self.runsc_version,
            "spec_fingerprint": self.spec_fingerprint,
            # The command, not a path: the executor is an installed wheel, so
            # there is no stable source file to point at. The worker replays
            # this verbatim rather than assembling its own.
            "executor_argv": list(self.executor_argv),
            # The wire format the rootfs's executor speaks. The worker refuses a
            # generation whose version it does not implement.
            "executor_protocol": self.executor_protocol,
            "python_path": self.python_path,
            "overlay": self.overlay,
            "network": self.network,
            "root_readonly": self.root_readonly,
            "created_at": self.created_at or utc_now(),
        }

    def write(self, directory: Path) -> Path:
        """Write ``generation.json`` and return its path."""
        path = directory / "generation.json"
        _write_json(path, self.to_json())
        return path


@dataclass(frozen=True, slots=True)
class CheckpointMeta:
    """What one checkpoint can be restored into.

    The worker reads this before attempting a restore, so a mismatch becomes a
    named, logged downgrade rather than an opaque failure minutes into a request.
    Duplicated from the generation on purpose: a checkpoint directory should be
    self-describing even in isolation.
    """

    checkpoint_id: str
    generation_id: str
    runsc_version: str
    spec_fingerprint: str
    rootfs_id: str
    imports: tuple[str, ...] = field(default_factory=tuple)
    producer: str = "pipeline"
    created_at: str = ""

    def to_json(self) -> dict[str, Any]:
        """Render to the shape worker/internal/artifact reads."""
        return {
            "checkpoint_id": self.checkpoint_id,
            "generation_id": self.generation_id,
            "producer": self.producer,
            "runsc_version": self.runsc_version,
            "spec_fingerprint": self.spec_fingerprint,
            "rootfs_id": self.rootfs_id,
            "imports": list(self.imports),
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
