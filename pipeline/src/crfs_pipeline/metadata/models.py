"""Measured package facts.

The previous code emitted ``disk_size_mb`` and ``load_time``, the committed data
has ``disk_size_mb`` and ``import_time``, and the TypedDict declared ``size``
and ``load_time``. Three names for two fields, and nothing noticed because a
TypedDict is a dict at runtime. ``import_time`` is what is actually on disk, so
that is what this reads -- accepting the other spelling rather than discarding
data that took a subprocess per package to measure.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class MetadataError(Exception):
    """The metadata on disk is not what this code expects."""


class ResourceType(StrEnum):
    """What kind of thing a metadata entry describes."""

    PACKAGE = "package"
    DATASET = "dataset"
    MODEL = "model"
    TOKENIZER = "tokenizer"


@dataclass(frozen=True, slots=True)
class PackageFacts:
    """What one package costs to have available."""

    base_import: str
    """Top-level import name, e.g. ``sklearn``."""

    distribution: str = ""
    """PyPI distribution name, e.g. ``scikit-learn``. Often differs."""

    dependencies: dict[str, str] = field(default_factory=dict)
    disk_size_mb: float = 0.0
    import_time: float = 0.0
    """Seconds to import with its dependencies already resident. This is what a
    checkpoint saves, and what the planner maximises."""

    resource_type: ResourceType = ResourceType.PACKAGE
    error: str = ""
    """Why this package could not be analysed, if it could not."""

    @property
    def usable(self) -> bool:
        """Whether the planner can score this package."""
        return not self.error

    @classmethod
    def from_json(cls, name: str, raw: object) -> PackageFacts:
        """Build facts from an on-disk entry.

        Takes object rather than a Mapping because the input is whatever
        json.loads produced: a file with a string where an object belongs must
        be reported, not assumed away.
        """
        if not isinstance(raw, Mapping):
            msg = f"metadata for {name!r} must be an object, got {type(raw).__name__}"
            raise MetadataError(msg)

        return cls(
            base_import=str(raw.get("base_import") or name),
            distribution=str(raw.get("distribution") or ""),
            dependencies=dict(raw.get("dependencies") or {}),
            disk_size_mb=float(raw.get("disk_size_mb") or 0.0),
            # Both spellings accepted: the committed data uses import_time and
            # the previous writer emitted load_time.
            import_time=float(raw.get("import_time") or raw.get("load_time") or 0.0),
            resource_type=ResourceType(raw.get("resource_type") or ResourceType.PACKAGE),
            error=str(raw.get("error") or ""),
        )

    def to_json(self) -> dict[str, Any]:
        """Render back to the on-disk shape.

        The key is ``resource_type``, not ``type``. The previous writer used the
        bare name ``type`` as a dict key, which is the *builtin*, so the field
        was never emitted at all -- confirmed absent from all 918 entries.
        """
        payload: dict[str, Any] = {
            "base_import": self.base_import,
            "distribution": self.distribution,
            "dependencies": dict(self.dependencies),
            "disk_size_mb": round(self.disk_size_mb, 4),
            "import_time": self.import_time,
            "resource_type": str(self.resource_type),
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class Metadata:
    """Facts for every package the corpus mentions."""

    packages: dict[str, PackageFacts] = field(default_factory=dict)

    def __len__(self) -> int:
        """How many packages are described."""
        return len(self.packages)

    def __contains__(self, name: str) -> bool:
        """Whether a package has facts."""
        return name in self.packages

    def get(self, name: str) -> PackageFacts | None:
        """Facts for a package, or None."""
        return self.packages.get(name)

    def size_mb(self, name: str) -> float:
        """Disk cost, or zero when unknown.

        Zero rather than an error: an unmeasured package should not stop a plan,
        it should just look free, and the planner's budget check still bounds
        the total.
        """
        facts = self.packages.get(name)
        return facts.disk_size_mb if facts else 0.0

    def import_time(self, name: str) -> float:
        """Import cost in seconds, or zero when unknown."""
        facts = self.packages.get(name)
        return facts.import_time if facts else 0.0

    @classmethod
    def load(cls, path: Path) -> Metadata:
        """Read and validate a metadata file."""
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError as exc:
            msg = f"no metadata at {path}; produce it with `crfs-pipeline analyze`"
            raise MetadataError(msg) from exc
        except json.JSONDecodeError as exc:
            msg = f"metadata at {path} is not valid JSON: {exc}"
            raise MetadataError(msg) from exc

        if not isinstance(raw, dict):
            msg = f"metadata at {path} must be an object, got {type(raw).__name__}"
            raise MetadataError(msg)

        return cls({name: PackageFacts.from_json(name, entry) for name, entry in raw.items()})

    def to_json(self) -> dict[str, Any]:
        """Render back to the on-disk shape."""
        return {name: facts.to_json() for name, facts in sorted(self.packages.items())}
