"""A planner that returns whatever it was given.

This is what the pipeline did before there was a planner: one checkpoint holding
pandas and numpy, regardless of the corpus. Kept because a fixed plan is the
right thing for a reproducibility run or a test that needs a known answer, and
because it makes the ``CheckpointPlanner`` seam concrete with something trivial.
"""

from __future__ import annotations

from collections.abc import Sequence

from readie_pipeline.corpus.models import Corpus
from readie_pipeline.metadata.models import Metadata
from readie_pipeline.planning.ports import Budget, CheckpointPlan


class FixedPlanner:
    """Returns a plan supplied at construction."""

    #: What the previous implementation hardcoded.
    DEFAULT: tuple[tuple[str, ...], ...] = (("pandas", "numpy"),)

    def __init__(self, sets: Sequence[Sequence[str]] | None = None) -> None:
        self._sets = tuple(tuple(s) for s in (sets if sets is not None else self.DEFAULT))

    def plan(self, corpus: Corpus, metadata: Metadata, budget: Budget) -> Sequence[CheckpointPlan]:
        """Return the configured sets, scored against the corpus.

        Scored even though it did not choose them: a fixed plan and a computed
        one are then comparable on the same numbers, which is the only way to
        tell whether the computed one is actually better.
        """
        plans = []
        for imports in self._sets[: budget.max_checkpoints]:
            wanted = set(imports)
            resolved_per_request = [metadata.resolve_imports(r.imports) for r in corpus]
            served = sum(1 for resolved in resolved_per_request if wanted & resolved)
            saved = sum(
                metadata.import_time(name)
                for resolved in resolved_per_request
                for name in wanted & resolved
            )
            plans.append(
                CheckpointPlan(
                    imports=imports,
                    requests_served=served,
                    seconds_saved=saved,
                    # Memory, not disk -- see GreedyPlanner._size_mb.
                    size_mb=sum(metadata.memory_size_mb(name) for name in imports),
                )
            )
        return plans
