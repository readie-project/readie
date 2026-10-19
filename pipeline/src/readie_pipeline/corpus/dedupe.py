"""Reject items that repeat one already in the corpus.

Three layers, cheapest first: the same ``ref`` (the same notebook fetched
twice), the same program after normalisation (formatting and comments do not
matter), and near-duplicates whose token shingles overlap heavily (a notebook
forked with a changed constant).
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Iterable

from readie_pipeline.corpus.extract import CodeError, parse
from readie_pipeline.corpus.models import CorpusItem

_TOKEN = re.compile(r"[A-Za-z_]\w*|\d+|\S")
_SHINGLE = 5
_SKETCH = 8


def normalise(code: Iterable[str]) -> str:
    """A form of the code that ignores formatting, comments and docstring text.

    Falls back to whitespace-collapsed text for a cell that does not parse, so
    the function never raises.
    """
    parts: list[str] = []
    for cell in code:
        try:
            parts.append(ast.dump(parse(cell), annotate_fields=False))
        except CodeError:
            parts.append(" ".join(cell.split()))
    return "\n".join(parts)


def fingerprint(code: Iterable[str]) -> str:
    """Hash of the normalised program."""
    return hashlib.sha256(normalise(code).encode()).hexdigest()


def _shingles(code: Iterable[str]) -> frozenset[int]:
    tokens = _TOKEN.findall("\n".join(code))
    if len(tokens) < _SHINGLE:
        return frozenset({hash(tuple(tokens))})
    return frozenset(
        hash(tuple(tokens[i : i + _SHINGLE])) for i in range(len(tokens) - _SHINGLE + 1)
    )


class DuplicateIndex:
    """Remembers what has been accepted and answers whether a candidate repeats it."""

    def __init__(self, threshold: float = 0.85) -> None:
        self._threshold = threshold
        self._refs: set[str] = set()
        self._fingerprints: set[str] = set()
        self._shingles: list[frozenset[int]] = []
        self._by_sketch: dict[int, list[int]] = {}

    @classmethod
    def of(cls, items: Iterable[CorpusItem], threshold: float = 0.85) -> DuplicateIndex:
        """An index already holding ``items``."""
        index = cls(threshold)
        for item in items:
            index.add(item.code, item.ref)
        return index

    def seen_ref(self, ref: str | None) -> bool:
        """Whether this origin was already fetched."""
        return ref is not None and ref in self._refs

    def is_duplicate(self, code: tuple[str, ...], ref: str | None = None) -> bool:
        """Whether the candidate repeats an accepted item."""
        if self.seen_ref(ref) or fingerprint(code) in self._fingerprints:
            return True
        shingles = _shingles(code)
        candidates = {i for h in sorted(shingles)[:_SKETCH] for i in self._by_sketch.get(h, ())}
        return any(
            self._jaccard(shingles, self._shingles[i]) >= self._threshold for i in candidates
        )

    def add(self, code: tuple[str, ...], ref: str | None = None) -> None:
        """Record an accepted item."""
        if ref is not None:
            self._refs.add(ref)
        self._fingerprints.add(fingerprint(code))
        shingles = _shingles(code)
        position = len(self._shingles)
        self._shingles.append(shingles)
        for h in sorted(shingles)[:_SKETCH]:
            self._by_sketch.setdefault(h, []).append(position)

    @staticmethod
    def _jaccard(a: frozenset[int], b: frozenset[int]) -> float:
        union = len(a | b)
        return len(a & b) / union if union else 1.0
