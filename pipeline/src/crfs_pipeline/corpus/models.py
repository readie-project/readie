"""The request corpus, as validated values.

Frozen dataclasses rather than ``TypedDict``. A TypedDict is a plain ``dict`` at
runtime and nothing checks it, which is how the previous schema came to declare
a ``code_snippet`` field that the data has never had -- every entry uses
``code``. An explicit ``from_json`` makes that a load-time error instead of a
silent ``KeyError`` in whatever reads it next.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class CorpusError(Exception):
    """The corpus on disk is not what this code expects."""


@dataclass(frozen=True, slots=True)
class Request:
    """One code snippet from the corpus."""

    task_name: str
    category: str
    code: str
    imports: tuple[str, ...] = field(default_factory=tuple)
    datasets: tuple[str, ...] = field(default_factory=tuple)
    models: tuple[str, ...] = field(default_factory=tuple)
    tokenizers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def top_level_imports(self) -> frozenset[str]:
        """Distribution-level names: ``sklearn.svm`` counts as ``sklearn``.

        The planner reasons about what has to be installed and imported, and
        that is the top-level package.
        """
        return frozenset(name.split(".")[0] for name in self.imports if name)

    @classmethod
    def from_json(cls, raw: Mapping[str, Any], *, index: int) -> Request:
        """Build a request, naming the offending entry when it cannot."""
        missing = {"task_name", "category", "code"} - raw.keys()
        if missing:
            msg = f"corpus entry {index} is missing {', '.join(sorted(missing))}"
            raise CorpusError(msg)

        return cls(
            task_name=str(raw["task_name"]),
            category=str(raw["category"]),
            code=str(raw["code"]),
            imports=_strings(raw.get("imports")),
            datasets=_strings(raw.get("datasets")),
            models=_strings(raw.get("models")),
            tokenizers=_strings(raw.get("tokenizers")),
        )

    def to_json(self) -> dict[str, Any]:
        """Render back to the on-disk shape."""
        return {
            "task_name": self.task_name,
            "category": self.category,
            "code": self.code,
            "imports": list(self.imports),
            "datasets": list(self.datasets),
            "models": list(self.models),
            "tokenizers": list(self.tokenizers),
        }


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str):
        msg = f"expected a list of strings, got {type(value).__name__}"
        raise CorpusError(msg)
    return tuple(str(v) for v in value)


@dataclass(frozen=True, slots=True)
class Corpus:
    """Every request the planner reasons over."""

    requests: tuple[Request, ...]

    def __len__(self) -> int:
        """How many requests."""
        return len(self.requests)

    def __iter__(self) -> Iterator[Request]:
        """Iterate the requests."""
        return iter(self.requests)

    @classmethod
    def load(cls, path: Path) -> Corpus:
        """Read and validate a corpus file."""
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError as exc:
            msg = f"no corpus at {path}; generate one with `crfs-pipeline corpus`"
            raise CorpusError(msg) from exc
        except json.JSONDecodeError as exc:
            msg = f"corpus at {path} is not valid JSON: {exc}"
            raise CorpusError(msg) from exc

        if not isinstance(raw, list):
            msg = f"corpus at {path} must be a list, got {type(raw).__name__}"
            raise CorpusError(msg)

        return cls(tuple(Request.from_json(entry, index=i) for i, entry in enumerate(raw)))

    @classmethod
    def of(cls, requests: Iterable[Request]) -> Corpus:
        """Build a corpus in memory. Mostly for tests."""
        return cls(tuple(requests))

    def import_counts(self) -> dict[str, int]:
        """How many requests use each top-level package, most common first.

        Counted per *request*, not per import statement: two `import pandas`
        lines in one snippet are one request that needs pandas.
        """
        counts: dict[str, int] = {}
        for request in self.requests:
            for name in request.top_level_imports:
                counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def category_counts(self) -> dict[str, int]:
        """How many requests fall in each category."""
        counts: dict[str, int] = {}
        for request in self.requests:
            counts[request.category] = counts.get(request.category, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def resources(self) -> Resources:
        """Every distinct resource the corpus refers to."""
        return Resources(
            packages=frozenset().union(*(r.top_level_imports for r in self.requests))
            if self.requests
            else frozenset(),
            datasets=frozenset(d for r in self.requests for d in r.datasets),
            models=frozenset(m for r in self.requests for m in r.models),
            tokenizers=frozenset(t for r in self.requests for t in r.tokenizers),
        )


@dataclass(frozen=True, slots=True)
class Resources:
    """The distinct resources a corpus refers to."""

    packages: frozenset[str] = frozenset()
    datasets: frozenset[str] = frozenset()
    models: frozenset[str] = frozenset()
    tokenizers: frozenset[str] = frozenset()
