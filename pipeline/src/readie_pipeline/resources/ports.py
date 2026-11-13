"""The analysis boundary implemented by each resource source."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from readie_pipeline.metadata.models import Metadata, ResourceFacts
from readie_pipeline.resources.packages import ColdProfile


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    """Results and profiling policy shared with an analysis handler."""

    measured: Metadata
    discovery: Metadata
    cold: ColdProfile
    report: Callable[[str, ResourceFacts], None]
    excluded: frozenset[str] = frozenset()


class ResourceAnalyzer(Protocol):
    """Resolve and measure one resource, including any discovered resources."""

    def requires(self, identifier: str) -> tuple[str, ...]:
        """Resources that must be measured before this one."""
        ...

    def analyze(self, identifier: str, context: AnalysisContext) -> Metadata:
        """Return source-qualified facts and resolutions."""
        ...
