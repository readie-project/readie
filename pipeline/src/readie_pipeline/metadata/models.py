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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name


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
    memory_size_mb: float = 0.0
    """Resident memory (RSS) this package adds once imported, with its
    dependencies already resident -- same incremental measurement as
    ``import_time``, not cumulative, so a closure sums it without
    double-counting a shared dependency. This, not ``disk_size_mb``, is what
    gVisor actually has to copy back on restore: a package's on-disk install
    footprint and its resident memory footprint are not proportional to each
    other (nltk measures ~10MB on disk but ~120MB resident, for example), so
    the planner and catalogue price checkpoints by this instead."""
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
            memory_size_mb=float(raw.get("memory_size_mb") or 0.0),
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
        was never emitted at all.

        Carries no ``error`` field, on the theory that an entry only reaches
        this method at all when ``Metadata.to_json`` chooses to keep it (see
        there) -- a package that could not be measured is dropped rather than
        written down as broken.
        """
        return {
            "base_import": self.base_import,
            "distribution": self.distribution,
            "dependencies": dict(self.dependencies),
            "disk_size_mb": round(self.disk_size_mb, 4),
            "memory_size_mb": round(self.memory_size_mb, 4),
            "import_time": self.import_time,
            "resource_type": str(self.resource_type),
        }


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

    def memory_size_mb(self, name: str) -> float:
        """Resident memory cost, or zero when unknown.

        What the planner and catalogue actually price a checkpoint by -- see
        ``PackageFacts.memory_size_mb``.
        """
        facts = self.packages.get(name)
        return facts.memory_size_mb if facts else 0.0

    def import_time(self, name: str) -> float:
        """Import cost in seconds, or zero when unknown."""
        facts = self.packages.get(name)
        return facts.import_time if facts else 0.0

    def _by_distribution(self) -> dict[str, str]:
        """PEP 503 canonical distribution name -> the import name it was analysed under.

        Canonicalized (lowercased, runs of ``-_.`` collapsed to one ``-``)
        because the same distribution is spelled inconsistently across a real
        base image: ``huggingface_hub`` records its own ``distribution`` as
        ``huggingface_hub``, but ``transformers``, ``datasets`` and others
        depend on it spelled ``huggingface-hub``. An exact-string map missed
        that match entirely, so the unresolved literal ``huggingface-hub``
        ended up in a checkpoint's ``imports`` and failed at restore time with
        ``ModuleNotFoundError: No module named 'huggingface-hub'`` -- a name
        with a hyphen was never importable to begin with.
        """
        return {
            canonicalize_name(facts.distribution): name
            for name, facts in self.packages.items()
            if facts.distribution
        }

    def direct_dependencies(self, name: str) -> frozenset[str]:
        """A package's measured dependencies, resolved to their import names.

        ``PackageFacts.dependencies`` is keyed by distribution name (e.g.
        ``scikit-learn``), because that is what a requirement names; this
        resolves each one back to whichever import name it was analysed under
        (via that entry's own ``distribution`` field, PEP 503 canonicalized so
        differing spellings of the same distribution still match -- see
        ``_by_distribution``), which is the name the planner and catalogue key
        everything else by. A dependency with no matching entry (never
        installed, or analysis failed for it) falls back to its distribution
        name so it still shows up in a closure rather than vanishing silently.
        """
        facts = self.packages.get(name)
        if facts is None:
            return frozenset()
        by_distribution = self._by_distribution()
        return frozenset(
            by_distribution.get(canonicalize_name(dist), dist) for dist in facts.dependencies
        )

    def closure(self, names: Iterable[str]) -> frozenset[str]:
        """Every name in ``names``, plus everything they transitively depend on.

        This is what a checkpoint actually has to hold resident for those
        packages to import at their measured (dependencies-already-loaded)
        cost: each dependency is walked once no matter how many packages in
        the closure share it, via ``seen``. The distribution/import-name map is
        built once per call rather than once per node -- this is walked once
        per distinct request in a corpus of thousands, so that difference is
        the one that matters.
        """
        by_distribution = self._by_distribution()
        seen: set[str] = set()
        stack = list(names)
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.add(name)
            facts = self.packages.get(name)
            if facts is None:
                continue
            for dist in facts.dependencies:
                dep = by_distribution.get(canonicalize_name(dist), dist)
                if dep not in seen:
                    stack.append(dep)
        return frozenset(seen)

    @classmethod
    def load(cls, path: Path) -> Metadata:
        """Read and validate a metadata file."""
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError as exc:
            msg = f"no metadata at {path}; produce it with `readie-pipeline analyze`"
            raise MetadataError(msg) from exc
        except json.JSONDecodeError as exc:
            msg = f"metadata at {path} is not valid JSON: {exc}"
            raise MetadataError(msg) from exc

        if not isinstance(raw, dict):
            msg = f"metadata at {path} must be an object, got {type(raw).__name__}"
            raise MetadataError(msg)

        return cls({name: PackageFacts.from_json(name, entry) for name, entry in raw.items()})

    def to_json(self) -> dict[str, Any]:
        """Render back to the on-disk shape.

        A package that could not be analysed is dropped rather than written
        down with its error: nothing downstream distinguishes "never seen"
        from "measured and broken" anyway (both cost 0.0, see ``size_mb`` and
        ``import_time`` above), so keeping it around on disk would only be
        clutter -- and, while analysing a real base image, actively misleading
        clutter, since a dependency name guessed wrong by ``top_level_imports``
        showed up here as if it were a real package that failed.
        """
        return {
            name: facts.to_json()
            for name, facts in sorted(self.packages.items())
            if not facts.error
        }
