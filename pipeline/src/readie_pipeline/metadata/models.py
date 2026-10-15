"""Measured resource facts.

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
    KAGGLE_DATASET = "kaggle_dataset"
    HF_DATASET = "hf_dataset"
    HF_MODEL = "hf_model"
    HF_TOKENIZER = "hf_tokenizer"

    @classmethod
    def from_key(cls, key: str) -> ResourceType:
        """Return the resource kind encoded in a corpus or catalogue key."""
        prefix, separator, _ = key.partition(":")
        if separator:
            legacy = {
                "dataset": cls.KAGGLE_DATASET,
                "model": cls.HF_MODEL,
                "tokenizer": cls.HF_TOKENIZER,
            }
            if prefix in legacy:
                return legacy[prefix]
            try:
                return cls(prefix)
            except ValueError:
                pass
        return cls.PACKAGE


@dataclass(frozen=True, slots=True)
class ResourceFacts:
    """What one resource costs to have available."""

    base_import: str
    """The canonical identifier for this resource."""

    distribution: str = ""
    """Best-effort package distribution name, when this is a package resource."""

    dependencies: frozenset[str] = frozenset()
    """Typed resource keys needed to load this resource."""

    loaded_modules: frozenset[str] | None = None
    """Deprecated constructor alias for package dependencies."""

    disk_size_mb: float = 0.0
    memory_size_mb: float = 0.0
    """Incremental resident memory added by loading this resource."""
    import_time: float = 0.0
    """Seconds spent loading this resource after its dependencies are ready."""

    resource_type: ResourceType = ResourceType.PACKAGE
    error: str = ""
    """Internal failure marker; failed resources are not persisted."""

    def __post_init__(self) -> None:
        """Normalize the deprecated package dependency constructor alias."""
        if self.loaded_modules is not None and not self.dependencies:
            object.__setattr__(
                self,
                "dependencies",
                frozenset(
                    value if ":" in value else f"package:{value}" for value in self.loaded_modules
                ),
            )

    @property
    def usable(self) -> bool:
        """Whether this resource can be used by downstream planning."""
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

        dependencies = raw.get("dependencies") or raw.get("loaded_modules") or ()
        if not isinstance(dependencies, (list, tuple, set, frozenset)):
            msg = f"dependencies for {name!r} must be a list of strings"
            raise MetadataError(msg)

        return cls(
            base_import=str(raw.get("base_import") or name),
            distribution=str(raw.get("distribution") or ""),
            dependencies=frozenset(
                value if ":" in str(value) else f"package:{value}" for value in dependencies
            ),
            disk_size_mb=float(raw.get("disk_size_mb") or 0.0),
            memory_size_mb=float(raw.get("memory_size_mb") or 0.0),
            # Both spellings accepted: the committed data uses import_time and
            # the previous writer emitted load_time.
            import_time=float(raw.get("import_time") or raw.get("load_time") or 0.0),
            resource_type=_resource_type_from_json(raw.get("resource_type")),
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
            "dependencies": sorted(self.dependencies),
            "disk_size_mb": round(self.disk_size_mb, 4),
            "memory_size_mb": round(self.memory_size_mb, 4),
            "import_time": self.import_time,
            "resource_type": str(self.resource_type),
        }


# Kept as an import compatibility alias while callers migrate to the common
# resource terminology.
PackageFacts = ResourceFacts


def _resource_type_from_json(value: object) -> ResourceType:
    """Read current and legacy resource type spellings."""
    if value in (None, ""):
        return ResourceType.PACKAGE
    if value == "dataset":
        return ResourceType.KAGGLE_DATASET
    if value == "model":
        return ResourceType.HF_MODEL
    if value == "tokenizer":
        return ResourceType.HF_TOKENIZER
    return ResourceType(str(value))


@dataclass(frozen=True, slots=True)
class Metadata:
    """Facts for every resource the corpus mentions."""

    resources: dict[str, ResourceFacts] = field(default_factory=dict)

    resolved: dict[str, str] = field(default_factory=dict)
    """Raw corpus import string -> the resolved dotted name it actually names
    (e.g. ``sklearn.svm.LinearSVC`` -> ``sklearn.svm``), populated by
    ``analyze()``. A raw string with no entry here never resolved to anything
    importable in the analysed environment (a hallucinated or uninstalled
    name) and is dropped rather than guessed at."""

    def __post_init__(self) -> None:
        """Normalize legacy bare resource keys to typed keys."""
        normalized = {
            name if ":" in name else f"{facts.resource_type}:{name}": facts
            for name, facts in self.resources.items()
        }
        if normalized != self.resources:
            object.__setattr__(self, "resources", normalized)

    @property
    def packages(self) -> dict[str, ResourceFacts]:
        """Compatibility view of the common resource mapping.

        Package-specific callers predate the common resource store. Keeping
        this view avoids changing those callers while datasets and packages
        are         serialized together under ``resources``.
        """
        return {
            name.partition(":")[2] if name.startswith("package:") else name: facts
            for name, facts in self.resources.items()
            if facts.resource_type == ResourceType.PACKAGE
        }

    def __len__(self) -> int:
        """How many resources are described."""
        return len(self.resources)

    def __contains__(self, name: str) -> bool:
        """Whether a package has facts."""
        return self._key(name) in self.resources

    def get(self, name: str) -> ResourceFacts | None:
        """Facts for a package, or None."""
        return self.resources.get(self._key(name)) or self.resources.get(name)

    @staticmethod
    def _key(name: str, resource_type: ResourceType = ResourceType.PACKAGE) -> str:
        """Normalize legacy bare names to typed resource keys."""
        return name if ":" in name else f"{resource_type}:{name}"

    def size_mb(self, name: str) -> float:
        """Disk cost, or zero when unknown.

        Zero rather than an error: an unmeasured package should not stop a plan,
        it should just look free, and the planner's budget check still bounds
        the total.
        """
        facts = self.get(name)
        return facts.disk_size_mb if facts else 0.0

    def memory_size_mb(self, name: str) -> float:
        """Resident memory cost, or zero when unknown.

        What the planner and catalogue actually price a checkpoint by -- see
        ``ResourceFacts.memory_size_mb``.
        """
        facts = self.get(name)
        return facts.memory_size_mb if facts else 0.0

    def import_time(self, name: str) -> float:
        """Import cost in seconds, or zero when unknown."""
        facts = self.get(name)
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
        facts = self.get(name)
        if facts is None:
            return frozenset()
        return facts.dependencies - {self._key(name)}

    def closure(self, names: Iterable[str]) -> frozenset[str]:
        """Every name in ``names``, plus everything each one empirically loads.

        Each ``ResourceFacts.dependencies`` is already fully transitive (see
        above), so this is a flat union, not a graph walk: nothing here can
        discover a name that was not already resident in whichever node's own
        ``sys.modules`` diff produced it.
        """
        seen: set[str] = {self._key(name) for name in names}
        for name in names:
            facts = self.get(name)
            if facts is not None:
                seen |= facts.dependencies
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
                facts = self.get(raw)
                if facts is not None and facts.usable:
                    name = raw.partition(":")[2] if ":" in raw else raw
            elif ":" in name:
                name = name.partition(":")[2]
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

        if "resources" in raw:
            resources_raw, resolved_raw = raw["resources"], raw.get("resolved", {})
        elif "packages" in raw:
            # Metadata written before the common resource store.
            resources_raw, resolved_raw = raw["packages"], raw.get("resolved", {})
        else:
            # Predates the "resolved" map: a flat {name: facts} dict.
            resources_raw, resolved_raw = raw, {}

        resources = {}
        for name, entry in resources_raw.items():
            facts = ResourceFacts.from_json(name, entry)
            key = name if ":" in name else f"{facts.resource_type}:{name}"
            resources[key] = facts
        return cls(
            resources,
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
            "resources": {
                name: facts.to_json()
                for name, facts in sorted(self.resources.items())
                if not facts.error
            },
            "resolved": dict(sorted(self.resolved.items())),
        }
