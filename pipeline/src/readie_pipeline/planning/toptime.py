"""Choose what to pre-import, by taking the closures that save the most import time.

Each distinct eligible closure is ranked once by the import time it would remove
across the corpus (the time of every package it shares with a request, weighted by
how often that request occurs). Closures are then taken in that order while they
fit the remaining size budget. Nothing is re-ranked after a pick, so overlapping
picks are not discounted; that is what separates this planner from greedy
selection, which scores each candidate by its marginal gain.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from readie_pipeline.corpus.models import CorpusItem
from readie_pipeline.metadata.models import Metadata
from readie_pipeline.planning._closures import membership
from readie_pipeline.planning.greedy import GreedyPlanner


class TopTimePlanner(GreedyPlanner):
    """Select checkpoint contents by standalone import-time reduction."""

    def _select_checkpoints(
        self,
        *,
        requests: Sequence[CorpusItem],
        eligible_packages: set[str],
        metadata: Metadata,
        size_budget_mb: float,
        max_checkpoints: int,
    ) -> list[frozenset[str]]:
        """Return up to ``max_checkpoints`` closures, biggest time reduction first."""
        unique_requests, occurrences = self._nodes(requests, eligible_packages, metadata)
        if not unique_requests:
            return []

        members, packages = membership(unique_requests)
        times = np.asarray([self._import_time(name, metadata) for name in packages], dtype=float)
        package_sizes = np.asarray(
            [self._size_mb(name, metadata) for name in packages], dtype=float
        )

        saved = ((members * times) @ members.T) @ occurrences
        sizes = members @ package_sizes

        selected: list[frozenset[str]] = []
        remaining = size_budget_mb
        # Stable sort: equal savings keep the closures' sorted order.
        for index in np.argsort(-saved, kind="stable"):
            if len(selected) == max_checkpoints or saved[index] <= 0:
                break
            if sizes[index] <= remaining:
                selected.append(unique_requests[int(index)])
                remaining -= float(sizes[index])
        return selected
