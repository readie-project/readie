"""What a planner is, and what it produces.

``CheckpointPlanner`` is a ``Protocol`` so a selection model can satisfy it
structurally -- including one living in another repository that must not import
this package.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from readie_pipeline.corpus.models import Corpus
from readie_pipeline.metadata.models import Metadata


@dataclass(frozen=True, slots=True)
class Budget:
    """What a plan may spend."""

    max_checkpoints: int = 8
    size_mb: float = 2048.0


@dataclass(frozen=True, slots=True)
class CheckpointPlan:
    """One checkpoint's contents.

    ``datasets``, ``models`` and ``tokenizers`` are carried through the schema
    and written to the plan, but nothing pre-loads them yet: only packages are
    imported before capture.
    """

    imports: tuple[str, ...] = field(default_factory=tuple)
    datasets: tuple[str, ...] = field(default_factory=tuple)
    models: tuple[str, ...] = field(default_factory=tuple)
    tokenizers: tuple[str, ...] = field(default_factory=tuple)

    # Why this checkpoint was chosen. Written to the plan so a later run can be
    # compared against an earlier one without rerunning the planner.
    requests_served: int = 0
    seconds_saved: float = 0.0
    size_mb: float = 0.0
    memory_size_mb: float = 0.0

    def to_json(self) -> dict[str, Any]:
        """Render to the on-disk plan shape."""
        return {
            "imports": list(self.imports),
            "datasets": list(self.datasets),
            "models": list(self.models),
            "tokenizers": list(self.tokenizers),
            "requests_served": self.requests_served,
            "seconds_saved": round(self.seconds_saved, 4),
            "size_mb": round(self.size_mb, 4),
            "memory_size_mb": round(self.memory_size_mb, 4),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> CheckpointPlan:
        """Read one entry back."""
        return cls(
            imports=tuple(raw.get("imports") or ()),
            datasets=tuple(raw.get("datasets") or ()),
            models=tuple(raw.get("models") or ()),
            tokenizers=tuple(raw.get("tokenizers") or ()),
            requests_served=int(raw.get("requests_served") or 0),
            seconds_saved=float(raw.get("seconds_saved") or 0.0),
            size_mb=float(raw.get("size_mb") or 0.0),
            memory_size_mb=float(raw.get("memory_size_mb") or 0.0),
        )


@runtime_checkable
class CheckpointPlanner(Protocol):
    """Chooses what each checkpoint should have pre-imported."""

    def plan(self, corpus: Corpus, metadata: Metadata, budget: Budget) -> Sequence[CheckpointPlan]:
        """Return the checkpoints worth capturing, best first."""
        ...
