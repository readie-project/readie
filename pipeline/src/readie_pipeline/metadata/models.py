"""Measured facts keyed by source-qualified resource identifiers."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from readie_pipeline.metadata.inventory import canonical_name
from readie_pipeline.resources.identifiers import ResourceType, parse_resource


class MetadataError(Exception):
    """The metadata on disk is not what this code expects."""


@dataclass(frozen=True, slots=True)
class ResourceFacts:
    """Measured loading costs shared by every resource type."""

    base_import: str
    """The resolved dotted import name, e.g. ``sklearn`` or ``sklearn.svm`` --
    not necessarily top-level: there is no top-level collapse any more, so
    ``sklearn`` and ``sklearn.svm`` are independent, separately-priced
    entries."""

    distribution: str = ""
    """PyPI distribution name, e.g. ``scikit-learn``. Best-effort only --
    resolved from the top-level segment of ``base_import`` via
    ``packages_distributions()``, kept for diagnostics, not load-bearing."""

    dependencies: frozenset[str] = frozenset()
    """Source-qualified dependencies, including modules observed during loading.

    For packages this folds the full empirical ``sys.modules`` difference into
    the dependency set; it is already transitive and retains dotted submodules.
    Dataset prerequisites and observed imports use the same identifiers.
    """

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
    def from_json(cls, name: str, raw: object) -> ResourceFacts:
        """Build facts from an on-disk entry.

        Takes object rather than a Mapping because the input is whatever
        json.loads produced: a file with a string where an object belongs must
        be reported, not assumed away.
        """
        if not isinstance(raw, Mapping):
            msg = f"metadata for {name!r} must be an object, got {type(raw).__name__}"
            raise MetadataError(msg)

        required = {
            "import_time",
            "memory_size_mb",
            "disk_size_mb",
            "dependencies",
            "resource_type",
        }
        if missing := required - raw.keys():
            msg = f"metadata for {name!r} is missing {', '.join(sorted(missing))}"
            raise MetadataError(msg)
        if "loaded_modules" in raw or "load_time" in raw:
            msg = f"metadata for {name!r} needs the one-time resource migration"
            raise MetadataError(msg)
        return cls(
            base_import=str(raw.get("base_import") or name),
            distribution=str(raw.get("distribution") or ""),
            dependencies=frozenset(raw.get("dependencies") or ()),
            disk_size_mb=float(raw.get("disk_size_mb") or 0.0),
            memory_size_mb=float(raw.get("memory_size_mb") or 0.0),
            import_time=float(raw["import_time"]),
            resource_type=ResourceType(raw["resource_type"]),
            error=str(raw.get("error") or ""),
        )

    def to_json(self) -> dict[str, Any]:
        """Serialize measurements without rounding or adding alternate field names."""
        return {
            "base_import": self.base_import,
            "distribution": self.distribution,
            "dependencies": sorted(self.dependencies),
            "disk_size_mb": self.disk_size_mb,
            "memory_size_mb": self.memory_size_mb,
            "import_time": self.import_time,
            "resource_type": str(self.resource_type),
        }


@dataclass(frozen=True, slots=True)
class Metadata:
    """Facts for measured resources."""

    resources: dict[str, ResourceFacts] = field(default_factory=dict)

    distributions: dict[str, str] = field(default_factory=dict)
    """Canonical distribution name -> the version installed in the image the
    metadata was measured in (``scikit-learn`` -> ``1.6.1``). One entry per
    distribution, not per package: many packages share a distribution. Empty for
    metadata measured before versions were recorded."""

    base_image: str = ""
    """The image the metadata was measured in, as a pullable reference (a digest,
    when the build pins one). Empty for metadata written before this was recorded,
    or measured where the image was not known."""

    resolved: dict[str, str] = field(default_factory=dict)
    """Raw corpus import string -> the resolved dotted name it actually names
    (e.g. ``sklearn.svm.LinearSVC`` -> ``sklearn.svm``), populated by
    ``analyze()``. A raw string with no entry here never resolved to anything
    importable in the analysed environment (a hallucinated or uninstalled
    name) and is dropped rather than guessed at."""

    def __len__(self) -> int:
        """How many packages are described."""
        return len(self.resources)

    def __contains__(self, name: str) -> bool:
        """Whether a package has facts."""
        return name in self.resources

    def get(self, name: str) -> ResourceFacts | None:
        """Facts for a package, or None."""
        return self.resources.get(name)

    def version_of(self, distribution: str) -> str:
        """The version recorded for ``distribution``, or ``""`` when there is none."""
        return self.distributions.get(canonical_name(distribution), "")

    def size_mb(self, name: str) -> float:
        """Disk cost, or zero when unknown.

        Zero rather than an error: an unmeasured package should not stop a plan,
        it should just look free, and the planner's budget check still bounds
        the total.
        """
        facts = self.resources.get(name)
        return facts.disk_size_mb if facts else 0.0

    def memory_size_mb(self, name: str) -> float:
        """Resident memory cost, or zero when unknown.

        What the planner and catalogue actually price a checkpoint by -- see
        ``ResourceFacts.memory_size_mb``.
        """
        facts = self.resources.get(name)
        return facts.memory_size_mb if facts else 0.0

    def import_time(self, name: str) -> float:
        """Import cost in seconds, or zero when unknown."""
        facts = self.resources.get(name)
        return facts.import_time if facts else 0.0

    def direct_dependencies(self, name: str) -> frozenset[str]:
        """Everything ``name``'s own import empirically loads, besides itself.

        ``ResourceFacts.dependencies`` is already ``name``'s full transitive
        closure (a single ``sys.modules`` diff around importing it captures
        its parent chain, internal submodules and external dependencies all
        at once), so there is no separate "direct" vs "transitive" distinction
        left to compute -- this is kept only as a convenience accessor for a
        single package.
        """
        facts = self.resources.get(name)
        if facts is None:
            return frozenset()
        return facts.dependencies - {name}

    def closure(self, names: Iterable[str]) -> frozenset[str]:
        """Union the already-transitive dependency sets without repeated graph walks."""
        requested = frozenset(names)
        seen = set(requested)
        for name in requested:
            facts = self.resources.get(name)
            if facts is not None:
                seen.update(facts.dependencies)
        return frozenset(seen)

    def resolve_resources(self, raw_imports: Iterable[str]) -> frozenset[str]:
        """Resolve qualified resource candidates to usable measured identifiers."""
        names: set[str] = set()
        for raw in raw_imports:
            if not raw:
                continue
            name = self.resolved.get(raw)
            if name is None:
                facts = self.resources.get(raw)
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

        if not isinstance(raw.get("resources"), dict):
            msg = f"metadata at {path} must contain a resources mapping"
            raise MetadataError(msg)
        entries = {}
        validated_dependencies: set[str] = set()
        for name, entry in raw["resources"].items():
            try:
                kind, _ = parse_resource(name)
                facts = ResourceFacts.from_json(name, entry)
                if facts.resource_type != kind:
                    msg = "resource type does not match its identifier"
                    raise MetadataError(msg)
                for dependency in facts.dependencies:
                    if dependency not in validated_dependencies:
                        parse_resource(dependency)
                        validated_dependencies.add(dependency)
            except (ValueError, TypeError, KeyError) as exc:
                msg = f"invalid metadata for {name!r}: {exc}"
                raise MetadataError(msg) from exc
            entries[name] = facts
        return cls(
            entries,
            distributions=dict(raw.get("distributions", {})),
            base_image=str(raw.get("base_image", "")),
            resolved=dict(raw.get("resolved", {})),
        )

    def save(self, path: Path) -> None:
        """Replace the finished file only after its new contents are written."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".writing")
        try:
            with temporary.open("w") as stream:
                json.dump(self.to_json(), stream, indent=2, sort_keys=True)
                stream.write("\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

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
            "base_image": self.base_image,
            "distributions": dict(sorted(self.distributions.items())),
            "resources": {
                name: facts.to_json()
                for name, facts in sorted(self.resources.items())
                if not facts.error
            },
            "resolved": dict(sorted(self.resolved.items())),
        }
