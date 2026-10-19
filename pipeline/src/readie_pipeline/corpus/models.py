"""The corpus, as validated values.

Each item is one workload: the code an execution (or a session of executions)
runs, the domain it belongs to, where it came from, and the resources it needs.
Frozen dataclasses rather than ``TypedDict`` so a malformed file is a load-time
error naming the entry instead of a ``KeyError`` in whatever reads it next.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from readie_pipeline.corpus.domains import DOMAINS
from readie_pipeline.metadata.models import Metadata


class CorpusError(Exception):
    """The corpus on disk is not what this code expects."""


class Source(StrEnum):
    """Where an item's code came from."""

    KAGGLE = "kaggle"
    KAGGLE_NOTEBOOK = "kaggle_notebook"
    HUGGINGFACE = "huggingface"
    HUGGINGFACE_NOTEBOOK = "huggingface_notebook"
    AGENT_SHORT = "agent_short"
    AGENT_NOTEBOOK = "agent_notebook"
    AGENT_FIX = "agent_fix"

    @property
    def fetched(self) -> bool:
        """True when the code is retrieved from a real service, not generated."""
        return not self.value.startswith("agent_")


class ResourceKind(StrEnum):
    """The kinds of resource an item can name."""

    PACKAGE = "package"
    HF_DATASET = "hf_dataset"
    HF_MODEL = "hf_model"
    HF_TOKENIZER = "hf_tokenizer"
    KAGGLE_DATASET = "kaggle_dataset"
    KAGGLE_MODEL = "kaggle_model"


def resource(kind: ResourceKind, value: str) -> str:
    """Format a resource as ``<kind>:<value>``."""
    return f"{kind.value}:{value}"


def parse_resource(text: str) -> tuple[ResourceKind, str]:
    """Split ``<kind>:<value>``, rejecting unknown kinds and empty values."""
    kind, sep, value = text.partition(":")
    try:
        parsed = ResourceKind(kind)
    except ValueError as exc:
        msg = f"unknown resource kind in {text!r}"
        raise CorpusError(msg) from exc
    if not sep or not value.strip():
        msg = f"resource {text!r} has no value"
        raise CorpusError(msg)
    return parsed, value


@dataclass(frozen=True, slots=True)
class CorpusItem:
    """One workload."""

    code: tuple[str, ...]
    """One string for a single execution; one string per step for notebooks,
    fix sessions and other multi-step sessions."""
    domain: str
    source: Source
    resources: tuple[str, ...] = field(default_factory=tuple)
    ref: str | None = None
    """Where fetched code came from (a URL); ``None`` for generated code."""
    original: tuple[str, ...] | None = None
    """The code as it was before stand-in data replaced its reads of missing
    files, kept so the rewrite can be undone; ``None`` when nothing was rewritten."""

    @property
    def imports(self) -> tuple[str, ...]:
        """Raw dotted import candidates, unresolved.

        Resolving a candidate to what it names needs an environment with the
        packages installed, so every reader goes through
        ``Metadata.resolve_imports`` instead of trusting these.
        """
        return self.of_kind(ResourceKind.PACKAGE)

    def of_kind(self, kind: ResourceKind) -> tuple[str, ...]:
        """Values of one kind of resource, in order."""
        prefix = f"{kind.value}:"
        return tuple(r[len(prefix) :] for r in self.resources if r.startswith(prefix))

    @classmethod
    def from_json(cls, raw: Mapping[str, Any], *, index: int) -> CorpusItem:
        """Build an item, naming the offending entry when it cannot."""
        missing = {"code", "domain", "source", "resources"} - raw.keys()
        if missing:
            msg = f"corpus entry {index} is missing {', '.join(sorted(missing))}"
            raise CorpusError(msg)

        code = _strings(raw["code"])
        if not code or not all(c.strip() for c in code):
            msg = f"corpus entry {index} has no code"
            raise CorpusError(msg)

        domain = str(raw["domain"])
        if domain not in DOMAINS:
            msg = f"corpus entry {index} has unknown domain {domain!r}"
            raise CorpusError(msg)

        try:
            source = Source(raw["source"])
        except ValueError as exc:
            msg = f"corpus entry {index} has unknown source {raw['source']!r}"
            raise CorpusError(msg) from exc

        resources = _strings(raw["resources"])
        for entry in resources:
            parse_resource(entry)

        ref = raw.get("ref")
        original = raw.get("original")
        return cls(
            code=code,
            domain=domain,
            source=source,
            resources=resources,
            ref=None if ref is None else str(ref),
            original=None if original is None else _strings(original),
        )

    def to_json(self) -> dict[str, Any]:
        """Render back to the on-disk shape."""
        out: dict[str, Any] = {
            "code": list(self.code),
            "domain": self.domain,
            "source": self.source.value,
            "resources": list(self.resources),
            "ref": self.ref,
        }
        if self.original is not None:
            out["original"] = list(self.original)
        return out


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        msg = f"expected a list of strings, got {type(value).__name__}"
        raise CorpusError(msg)
    return tuple(str(v) for v in value)


@dataclass(frozen=True, slots=True)
class Corpus:
    """Every item the planner reasons over."""

    items: tuple[CorpusItem, ...]

    def __len__(self) -> int:
        """How many items."""
        return len(self.items)

    def __iter__(self) -> Iterator[CorpusItem]:
        """Iterate the items."""
        return iter(self.items)

    @classmethod
    def load(cls, path: Path) -> Corpus:
        """Read and validate a corpus file."""
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError as exc:
            msg = f"no corpus at {path}; generate one with `readie-pipeline corpus`"
            raise CorpusError(msg) from exc
        except json.JSONDecodeError as exc:
            msg = f"corpus at {path} is not valid JSON: {exc}"
            raise CorpusError(msg) from exc

        if not isinstance(raw, list):
            msg = f"corpus at {path} must be a list, got {type(raw).__name__}"
            raise CorpusError(msg)

        return cls(tuple(CorpusItem.from_json(entry, index=i) for i, entry in enumerate(raw)))

    @classmethod
    def of(cls, items: Iterable[CorpusItem]) -> Corpus:
        """Build a corpus in memory."""
        return cls(tuple(items))

    def save(self, path: Path) -> None:
        """Write atomically, so an interrupted run never leaves a torn file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".partial")
        temp.write_text(json.dumps([i.to_json() for i in self.items], indent=2) + "\n")
        temp.replace(path)

    def import_counts(self, metadata: Metadata) -> dict[str, int]:
        """How many items use each resolved import, most common first.

        Counted per item: two ``import pandas`` lines, or the same import in
        two notebook cells, are one item that needs pandas.
        """
        counts: dict[str, int] = {}
        for item in self.items:
            for name in metadata.resolve_imports(item.imports):
                counts[name] = counts.get(name, 0) + 1
        return _ranked(counts)

    def domain_counts(self) -> dict[str, int]:
        """How many items fall in each domain."""
        return _ranked(_tally(i.domain for i in self.items))

    def source_counts(self) -> dict[str, int]:
        """How many items come from each source."""
        return _ranked(_tally(i.source.value for i in self.items))

    def package_usage(self) -> dict[str, int]:
        """How many items use each third-party package, most used first.

        Counted by top-level package (``scipy.signal.butter`` is ``scipy``) and
        once per item. The standard library is left out because it says nothing
        about how mainstream a workload is.
        """
        return _ranked(
            _tally(
                top
                for item in self.items
                for top in {name.split(".", maxsplit=1)[0] for name in item.imports}
                if top not in sys.stdlib_module_names
            )
        )

    def cell_counts(self) -> dict[tuple[str, Source], int]:
        """How many items fall in each (domain, source) pair."""
        counts: dict[tuple[str, Source], int] = {}
        for item in self.items:
            key = (item.domain, item.source)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def resources(self, metadata: Metadata) -> Resources:
        """Every distinct resource the corpus refers to."""
        packages: set[str] = set()
        for item in self.items:
            packages.update(metadata.resolve_imports(item.imports))
        return Resources(
            packages=frozenset(packages),
            datasets=frozenset(
                v
                for i in self.items
                for k in (ResourceKind.HF_DATASET, ResourceKind.KAGGLE_DATASET)
                for v in i.of_kind(k)
            ),
            models=frozenset(
                v
                for i in self.items
                for k in (ResourceKind.HF_MODEL, ResourceKind.KAGGLE_MODEL)
                for v in i.of_kind(k)
            ),
            tokenizers=frozenset(
                v for i in self.items for v in i.of_kind(ResourceKind.HF_TOKENIZER)
            ),
        )


def _tally(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _ranked(counts: dict[str, int]) -> dict[str, int]:
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


@dataclass(frozen=True, slots=True)
class Resources:
    """The distinct resources a corpus refers to."""

    packages: frozenset[str] = frozenset()
    datasets: frozenset[str] = frozenset()
    models: frozenset[str] = frozenset()
    tokenizers: frozenset[str] = frozenset()
