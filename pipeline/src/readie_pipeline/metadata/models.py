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
    """The resolved dotted import name, e.g. ``sklearn`` or ``sklearn.svm`` --
    not necessarily top-level: there is no top-level collapse any more, so
    ``sklearn`` and ``sklearn.svm`` are independent, separately-priced
    entries."""

    distribution: str = ""
    """PyPI distribution name, e.g. ``scikit-learn``. Best-effort only --
    resolved from the top-level segment of ``base_import`` via
    ``packages_distributions()``, kept for diagnostics, not load-bearing."""

    loaded_modules: frozenset[str] = frozenset()
    """Every module actually found resident in ``sys.modules`` after
    importing ``base_import`` in a clean process, dependencies included --
    this package's full, empirically observed, already-transitive closure.
    Replaces a walk over declared ``Requires-Dist`` metadata, which recorded
    what a package might need, not what a specific import actually loads."""

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
            loaded_modules=frozenset(raw.get("loaded_modules") or ()),
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
            "loaded_modules": sorted(self.loaded_modules),
            "disk_size_mb": round(self.disk_size_mb, 4),
            "memory_size_mb": round(self.memory_size_mb, 4),
            "import_time": self.import_time,
            "resource_type": str(self.resource_type),
        }


@dataclass(frozen=True, slots=True)
class Metadata:
    """Facts for every package the corpus mentions."""

    packages: dict[str, PackageFacts] = field(default_factory=dict)

    resolved: dict[str, str] = field(default_factory=dict)
    """Raw corpus import string -> the resolved dotted name it actually names
    (e.g. ``sklearn.svm.LinearSVC`` -> ``sklearn.svm``), populated by
    ``analyze()``. A raw string with no entry here never resolved to anything
    importable in the analysed environment (a hallucinated or uninstalled
    name) and is dropped rather than guessed at."""

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

    def direct_dependencies(self, name: str) -> frozenset[str]:
        """Everything ``name``'s own import empirically loads, besides itself.

        ``PackageFacts.loaded_modules`` is already ``name``'s full transitive
        closure (a single ``sys.modules`` diff around importing it captures
        its parent chain, internal submodules and external dependencies all
        at once), so there is no separate "direct" vs "transitive" distinction
        left to compute -- this is kept only as a convenience accessor for a
        single package.
        """
        facts = self.packages.get(name)
        if facts is None:
            return frozenset()
        return facts.loaded_modules - {name}

    def closure(self, names: Iterable[str]) -> frozenset[str]:
        """Every name in ``names``, plus everything each one empirically loads.

        Each ``PackageFacts.loaded_modules`` is already fully transitive (see
        above), so this is a flat union, not a graph walk: nothing here can
        discover a name that was not already resident in whichever node's own
        ``sys.modules`` diff produced it.
        """
        seen: set[str] = set(names)
        for name in names:
            facts = self.packages.get(name)
            if facts is not None:
                seen |= facts.loaded_modules
        return frozenset(seen)

    def resolve_imports(self, raw_imports: Iterable[str]) -> frozenset[str]:
        """Raw corpus import strings, mapped to their resolved canonical names.

        ``resolved`` is authoritative when present -- it is what ``analyze()``
        actually observed a raw candidate to resolve to. When it has nothing
        for a raw string (metadata built by hand, e.g. in a test, or written
        by something that predates this mapping), that raw string is instead
        used as its own name exactly when it already has usable facts under
        that same key -- covering metadata that already keys packages by
        their plain, correct name directly. A raw string with neither is
        dropped rather than passed through unresolved and risking a
        ``ModuleNotFoundError`` at restore time -- in particular, this is
        also what makes an ``analyze()``-recorded resolution failure (an error
        entry keyed by the raw string itself) drop out here rather than be
        mistaken for an already-resolved name.
        """
        names: set[str] = set()
        for raw in raw_imports:
            if not raw:
                continue
            name = self.resolved.get(raw)
            if name is None:
                facts = self.packages.get(raw)
                if facts is not None and facts.usable:
                    name = raw
            if name is not None:
                names.add(name)
        return frozenset(names)

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

        if "packages" in raw:
            packages_raw, resolved_raw = raw["packages"], raw.get("resolved", {})
        else:
            # Predates the "resolved" map: a flat {name: facts} dict.
            packages_raw, resolved_raw = raw, {}

        return cls(
            {name: PackageFacts.from_json(name, entry) for name, entry in packages_raw.items()},
            resolved={str(k): str(v) for k, v in resolved_raw.items()},
        )

    def to_json(self) -> dict[str, Any]:
        """Render back to the on-disk shape.

        A package that could not be analysed is dropped rather than written
        down with its error: nothing downstream distinguishes "never seen"
        from "measured and broken" anyway (both cost 0.0, see ``size_mb`` and
        ``import_time`` above), so keeping it around on disk would only be
        clutter -- and, while analysing a real base image, actively misleading
        clutter, since a raw import string that never resolved to anything
        would otherwise show up here as if it were a real package that failed.
        """
        return {
            "packages": {
                name: facts.to_json()
                for name, facts in sorted(self.packages.items())
                if not facts.error
            },
            "resolved": dict(sorted(self.resolved.items())),
        }
