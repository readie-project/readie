"""Choose what to pre-import, by k-medoids over the corpus's import closures.

Facilities and nodes are the same distinct eligible closures the greedy planner
uses, priced by the same distance (``alpha * size`` plus the import time a
checkpoint leaves unpaid), so the plan is directly comparable to greedy's. The
search starts from the greedy solution and a few seeded random sets, improves
each by swap and add moves, and keeps the cheapest. The seed is a constructor
argument, so a run is still reproducible.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from readie_pipeline.corpus.models import CorpusItem
from readie_pipeline.metadata.models import Metadata
from readie_pipeline.planning._closures import order_by_weight
from readie_pipeline.planning.greedy import MIN_COVERAGE, GreedyPlanner

#: Restarts beyond the greedy start.
RESTARTS = 3


class KMedoidsPlanner(GreedyPlanner):
    """Select checkpoint contents by PAM-style local search over closures."""

    def __init__(
        self,
        *,
        alpha: float,
        min_coverage: float = MIN_COVERAGE,
        restarts: int = RESTARTS,
        seed: int = 0,
    ) -> None:
        super().__init__(min_coverage=min_coverage, alpha=alpha)
        self._restarts = restarts
        self._seed = seed

    def _select_checkpoints(
        self,
        *,
        requests: Sequence[CorpusItem],
        eligible_packages: set[str],
        metadata: Metadata,
        size_budget_mb: float,
        max_checkpoints: int,
    ) -> list[frozenset[str]]:
        """Return up to ``max_checkpoints`` closures within the shared size budget."""
        unique_requests, occurrences = self._nodes(requests, eligible_packages, metadata)
        if not unique_requests:
            return []

        sizes = np.asarray(
            [sum(self._size_mb(name, metadata) for name in closure) for closure in unique_requests],
            dtype=float,
        )
        if not (sizes <= size_budget_mb).any():
            return []

        dist = self._distances(unique_requests, sizes, metadata)
        rng = np.random.default_rng(self._seed)

        starts = [self._greedy_start(dist, sizes, occurrences, size_budget_mb, max_checkpoints)]
        starts += [
            self._random_start(rng, sizes, size_budget_mb, max_checkpoints)
            for _ in range(self._restarts)
        ]

        best: list[int] = []
        best_score = np.inf
        for start in starts:
            medoids = self._local_search(
                start,
                dist=dist,
                sizes=sizes,
                occurrences=occurrences,
                budget=size_budget_mb,
                cap=max_checkpoints,
            )
            score = self._score(dist, occurrences, medoids)
            if score < best_score - _tolerance(best_score):
                best, best_score = medoids, score

        nearest = np.argmin(dist[best], axis=0)
        weights = [float(occurrences[nearest == i].sum()) for i in range(len(best))]
        return [unique_requests[best[i]] for i in order_by_weight(range(len(best)), weights)]

    @staticmethod
    def _score(dist: np.ndarray, occurrences: np.ndarray, medoids: Sequence[int]) -> float:
        """Occurrence-weighted distance of every node to its nearest medoid."""
        if not medoids:
            return float("inf")
        return float((dist[list(medoids)].min(axis=0) * occurrences).sum())

    def _greedy_start(
        self,
        dist: np.ndarray,
        sizes: np.ndarray,
        occurrences: np.ndarray,
        budget: float,
        cap: int,
    ) -> list[int]:
        """Add the facility that lowers the score most, until nothing improves or nothing fits."""
        medoids: list[int] = []
        best_dist = np.full(dist.shape[1], np.inf)
        score = np.inf
        while len(medoids) < cap:
            total = sizes[medoids].sum()
            fits = (sizes + total <= budget) & ~np.isin(np.arange(len(sizes)), medoids)
            if not fits.any():
                break
            scores = (np.minimum(best_dist[None, :], dist) * occurrences).sum(axis=1)
            scores[~fits] = np.inf
            pick = int(np.argmin(scores))
            # The first facility is always taken: there is no baseline to weigh it against.
            if medoids and scores[pick] >= score - _tolerance(score):
                break
            medoids.append(pick)
            best_dist = np.minimum(best_dist, dist[pick])
            score = float(scores[pick])
        return medoids

    @staticmethod
    def _random_start(
        rng: np.random.Generator, sizes: np.ndarray, budget: float, cap: int
    ) -> list[int]:
        """A seeded random set that fits the budget."""
        medoids: list[int] = []
        total = 0.0
        for candidate in rng.permutation(len(sizes)):
            if len(medoids) == cap:
                break
            if total + sizes[candidate] <= budget:
                medoids.append(int(candidate))
                total += float(sizes[candidate])
        return medoids

    def _local_search(
        self,
        start: list[int],
        *,
        dist: np.ndarray,
        sizes: np.ndarray,
        occurrences: np.ndarray,
        budget: float,
        cap: int,
    ) -> list[int]:
        """Apply the best improving swap or add until none improves."""
        medoids = list(start)
        if not medoids:
            return medoids
        everything = np.arange(len(sizes))

        while True:
            score = self._score(dist, occurrences, medoids)
            total = float(sizes[medoids].sum())
            best_score = score
            move: tuple[int | None, int] | None = None  # (position to replace or None to add, new)

            for position in range(len(medoids)):
                rest = medoids[:position] + medoids[position + 1 :]
                base = dist[rest].min(axis=0) if rest else np.full(dist.shape[1], np.inf)
                scores = (np.minimum(base[None, :], dist) * occurrences).sum(axis=1)
                fits = (total - sizes[medoids[position]] + sizes <= budget) & ~np.isin(
                    everything, medoids
                )
                scores[~fits] = np.inf
                pick = int(np.argmin(scores))
                if scores[pick] < best_score - _tolerance(best_score):
                    best_score, move = float(scores[pick]), (position, pick)

            if len(medoids) < cap:
                base = dist[medoids].min(axis=0)
                scores = (np.minimum(base[None, :], dist) * occurrences).sum(axis=1)
                fits = (total + sizes <= budget) & ~np.isin(everything, medoids)
                scores[~fits] = np.inf
                pick = int(np.argmin(scores))
                if scores[pick] < best_score - _tolerance(best_score):
                    best_score, move = float(scores[pick]), (None, pick)

            if move is None:
                return medoids
            slot, chosen = move
            if slot is None:
                medoids.append(chosen)
            else:
                medoids[slot] = chosen


def _tolerance(score: float) -> float:
    """Improvements smaller than float noise do not count, so the search terminates."""
    return 1e-12 * max(1.0, abs(score)) if np.isfinite(score) else 0.0
