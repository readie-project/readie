"""Choose what to pre-import, by clustering import closures on Jaccard similarity.

Two closures are close when they share most of their packages
(``1 - |a & b| / |a | b|``). The corpus's distinct eligible closures are grouped
into at most ``max_checkpoints`` clusters, and each cluster becomes one checkpoint
holding the union of its members. A union of closures is itself dependency-closed.

When a union does not fit the remaining size budget, the lightest members are
dropped from that cluster until it does, so the budget is shared across checkpoints
exactly as it is for the other planners. Clustering looks only at package overlap;
import time and size enter through the budget and the scoring ``plan`` applies.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from readie_pipeline.corpus.models import CorpusItem
from readie_pipeline.metadata.models import Metadata
from readie_pipeline.planning._closures import jaccard_distances
from readie_pipeline.planning.greedy import GreedyPlanner

#: Safety bound on reassignment rounds of the medoid search.
MAX_ROUNDS = 100


class _JaccardPlanner(GreedyPlanner):
    """Shared selection: cluster, take each cluster's union, fit the budget."""

    def _cluster(self, dist: np.ndarray, occurrences: np.ndarray, k: int) -> list[list[int]]:
        """Group node indices into at most ``k`` clusters."""
        raise NotImplementedError

    def _select_checkpoints(
        self,
        *,
        requests: Sequence[CorpusItem],
        eligible_packages: set[str],
        metadata: Metadata,
        size_budget_mb: float,
        max_checkpoints: int,
    ) -> list[frozenset[str]]:
        """Return one union-of-cluster checkpoint per cluster that fits, heaviest first."""
        unique_requests, occurrences = self._nodes(requests, eligible_packages, metadata)
        if not unique_requests:
            return []

        k = min(max_checkpoints, len(unique_requests))
        clusters = self._cluster(jaccard_distances(unique_requests), occurrences, k)
        clusters.sort(key=lambda c: (-float(occurrences[c].sum()), min(c)))

        selected: list[frozenset[str]] = []
        remaining = size_budget_mb
        for cluster in clusters:
            # Heaviest first, so popping from the end drops the lightest member.
            members = sorted(cluster, key=lambda i: (-occurrences[i], i))
            while members:
                union = frozenset().union(*(unique_requests[i] for i in members))
                size = sum(self._size_mb(name, metadata) for name in union)
                if size <= remaining:
                    selected.append(union)
                    remaining -= size
                    break
                members.pop()
        return selected


class JaccardAgglomerativePlanner(_JaccardPlanner):
    """Cluster closures bottom-up by weighted average linkage on Jaccard distance."""

    def _cluster(self, dist: np.ndarray, occurrences: np.ndarray, k: int) -> list[list[int]]:
        """Merge the two nearest clusters until ``k`` remain.

        Linkage is the occurrence-weighted mean of the pairwise distances (the
        Lance-Williams update), so a closure that many requests share pulls its
        cluster harder than a rare one. A cached nearest neighbour per row keeps a
        merge to a few vector operations.
        """
        n = len(occurrences)
        linkage = dist.astype(float).copy()
        np.fill_diagonal(linkage, np.inf)
        weights = occurrences.astype(float).copy()
        members: list[list[int]] = [[i] for i in range(n)]
        active = np.ones(n, dtype=bool)

        nearest = linkage.argmin(axis=1)
        nearest_dist = linkage[np.arange(n), nearest]

        for _ in range(n - k):
            i = int(np.argmin(np.where(active, nearest_dist, np.inf)))
            j = int(nearest[i])
            i, j = min(i, j), max(i, j)

            merged = (weights[i] * linkage[i] + weights[j] * linkage[j]) / (weights[i] + weights[j])
            linkage[i, :] = merged
            linkage[:, i] = merged
            linkage[i, i] = np.inf
            linkage[j, :] = np.inf
            linkage[:, j] = np.inf
            weights[i] += weights[j]
            members[i] += members[j]
            active[j] = False
            nearest_dist[j] = np.inf

            stale = active & ((nearest == i) | (nearest == j))
            stale[i] = True
            closer = active & ~stale & (linkage[:, i] < nearest_dist)
            nearest[closer] = i
            nearest_dist[closer] = linkage[closer, i]
            for row in np.flatnonzero(stale):
                nearest[row] = int(linkage[row].argmin())
                nearest_dist[row] = linkage[row, nearest[row]]

        return [sorted(members[i]) for i in np.flatnonzero(active)]
