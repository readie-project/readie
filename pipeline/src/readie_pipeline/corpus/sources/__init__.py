"""Where candidate workloads come from."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from readie_pipeline.corpus.models import Source


@dataclass(frozen=True, slots=True)
class Candidate:
    """Code that might become a corpus item, before any check."""

    code: tuple[str, ...]
    source: Source
    domain: str
    ref: str | None = None
    declared: tuple[str, ...] = field(default_factory=tuple)
    """Resources the origin states outright (for example the datasets a Kaggle
    notebook is attached to), as ``<kind>:<value>`` strings. They are verified
    like any resource found in the code."""


class CodeSource(Protocol):
    """Produces candidates for one or more :class:`Source` values."""

    def fetch(
        self,
        source: Source,
        domain: str,
        count: int,
        flavor: str,
        known_ref: Callable[[str | None], bool],
    ) -> list[Candidate]:
        """Return up to ``count`` candidates, skipping refs ``known_ref`` accepts.

        An empty list means the source has nothing more for this request.
        """
        ...


@runtime_checkable
class Repairer(Protocol):
    """A source that can rewrite a candidate whose resources were not real."""

    def repair(self, candidate: Candidate, failures: list[str], flavor: str) -> Candidate | None:
        """Return a corrected candidate, or ``None`` when it cannot be fixed."""
        ...


@runtime_checkable
class Broadens(Protocol):
    """A source whose search can be widened when it stops finding anything new."""

    def broaden(self, source: Source, domain: str, flavor: str, examples: Sequence[str]) -> int:
        """Add search phrases for the domain; return how many new ones were added."""
        ...
