"""A fake fetcher returning canned candidates, so tests need no Kaggle/HF access."""

from __future__ import annotations

from readie_evals.models import RawCandidate


class FakeFetcher:
    """Serves preset candidates per category up to the requested count."""

    def __init__(self, source: str, by_category: dict[str, list[RawCandidate]]) -> None:
        self.source = source
        self._by_category = by_category

    def fetch(self, *, category: str, count: int) -> list[RawCandidate]:
        return list(self._by_category.get(category, []))[:count]
