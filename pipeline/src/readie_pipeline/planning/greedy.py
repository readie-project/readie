"""Choose what to pre-import, by greedy facility selection.

No randomness, and every tie breaks on the package name. The same corpus and
metadata always produce a byte-identical plan, which is what lets a plan be
reviewed in a diff and pinned by a golden test.
"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Sequence

import numpy as np

from readie_pipeline.corpus.models import Corpus, Request
from readie_pipeline.metadata.models import Metadata
from readie_pipeline.planning.ports import Budget, CheckpointPlan

#: Standard library modules are never worth a checkpoint slot. They install
#: nothing, several are already imported before user code runs, and the rest
#: cost microseconds -- but with no measured import time they fall back to the
#: default below, which multiplied across thousands of requests outranks the
#: packages that actually matter. `json` and `collections` won slots this way.
STDLIB = frozenset(sys.stdlib_module_names)

#: A package used by fewer than this fraction of requests is not worth a
#: checkpoint slot, however cheap it is: the tail of the corpus is
#: mostly packages used once, and including them crowds out the ones that matter.
MIN_COVERAGE = 0.002

#: Packages with no measured import time still cost disk. Charging them a
#: nominal saving keeps a package the analysis could not profile from being
#: treated as literally worthless -- but small enough that a measured package
#: always wins.
UNMEASURED_IMPORT_TIME = 0.01

#: Every package occupies at least this much of the budget, so a package
#: reported as 0 MB cannot look free and be added without limit.
MIN_SIZE_MB = 0.5


class GreedyPlanner:
    """Select checkpoint contents using greedy facility selection."""

    def __init__(
        self,
        *,
        min_coverage: float = MIN_COVERAGE,
        alpha: float,
    ) -> None:
        self._min_coverage = min_coverage
        self._alpha = alpha

    def plan(
        self,
        corpus: Corpus,
        metadata: Metadata,
        budget: Budget,
    ) -> Sequence[CheckpointPlan]:
        """Choose up to ``budget.max_checkpoints`` checkpoints."""
        requests = corpus.requests
        if not requests:
            return []

        eligible_packages = self._eligible_packages(requests, metadata)
        if not eligible_packages:
            return []

        remaining_budget = budget.size_mb
        remaining_requests = list(requests)
        plans: list[CheckpointPlan] = []

        for _ in range(budget.max_checkpoints):
            if remaining_budget < MIN_SIZE_MB:
                break

            selected = self._select_checkpoint(
                requests=remaining_requests,
                eligible_packages=eligible_packages,
                metadata=metadata,
                size_budget_mb=remaining_budget,
            )

            if not selected:
                break

            plan = self._describe(
                selected=selected,
                requests=requests,
                metadata=metadata,
            )

            if plan.size_mb > remaining_budget:
                break

            plans.append(plan)
            remaining_budget -= plan.size_mb

            # The closure, not plan.imports: a request is fully covered once
            # this checkpoint's actual resident set (dependencies included)
            # is a superset of what it needs, regardless of which of those
            # names plan.imports trims out of the executor-facing list.
            closures = self._closures(remaining_requests, metadata)
            next_requests = [
                request
                for request in remaining_requests
                if not selected.issuperset(closures[request.top_level_imports])
            ]

            if len(next_requests) == len(remaining_requests):
                break

            remaining_requests = next_requests

            if not remaining_requests:
                break

        return plans

    @staticmethod
    def _closures(
        requests: Sequence[Request],
        metadata: Metadata,
    ) -> dict[frozenset[str], frozenset[str]]:
        """Each distinct request import set's dependency closure, computed once.

        A corpus repeats the same import set across many requests -- many
        snippets do ``import pandas, numpy`` -- so memoising on the set itself,
        not the request, keeps this to one closure walk per distinct
        combination instead of one per request.
        """
        cache: dict[frozenset[str], frozenset[str]] = {}
        for request in requests:
            imports = request.top_level_imports
            if imports not in cache:
                cache[imports] = metadata.closure(imports)
        return cache

    def _eligible_packages(
        self,
        requests: Sequence[Request],
        metadata: Metadata,
    ) -> set[str]:
        closures = self._closures(requests, metadata)
        users: dict[str, set[int]] = {}

        for index, request in enumerate(requests):
            for name in closures[request.top_level_imports]:
                if name in STDLIB:
                    continue

                users.setdefault(name, set()).add(index)

        floor = max(
            1,
            int(len(requests) * self._min_coverage),
        )

        return {name for name, request_indices in users.items() if len(request_indices) >= floor}

    @staticmethod
    def _import_time(
        name: str,
        metadata: Metadata,
    ) -> float:
        return metadata.import_time(name) or UNMEASURED_IMPORT_TIME

    @staticmethod
    def _size_mb(
        name: str,
        metadata: Metadata,
    ) -> float:
        """Apply the old planner's minimum package size."""
        return max(
            metadata.size_mb(name),
            MIN_SIZE_MB,
        )

    def _select_checkpoint(
        self,
        *,
        requests: Sequence[Request],
        eligible_packages: set[str],
        metadata: Metadata,
        size_budget_mb: float,
    ) -> frozenset[str]:
        closures = self._closures(requests, metadata)
        normalized_requests: list[frozenset[str]] = []

        for request in requests:
            imports = frozenset(
                name for name in closures[request.top_level_imports] if name in eligible_packages
            )

            if imports:
                normalized_requests.append(imports)

        if not normalized_requests:
            return frozenset()

        counts: Counter[frozenset[str]] = Counter(normalized_requests)

        unique_requests = sorted(
            counts,
            key=lambda request: tuple(sorted(request)),
        )

        node_occurrences = np.asarray(
            [counts[request] for request in unique_requests],
            dtype=float,
        )

        num_facilities = len(unique_requests)
        num_nodes = len(unique_requests)

        all_facility_sizes = np.asarray(
            [
                sum(self._size_mb(package, metadata) for package in request)
                for request in unique_requests
            ],
            dtype=float,
        )

        def node_to_facility_distance(
            facility_idx: int,
            node_idx: int,
        ) -> float:
            facility_names = unique_requests[facility_idx]
            node_names = unique_requests[node_idx]

            residual = node_names.difference(facility_names)

            return float(
                self._alpha * all_facility_sizes[facility_idx]
                + sum(self._import_time(package, metadata) for package in residual)
            )

        dist = np.empty(
            (num_facilities, num_nodes),
            dtype=float,
        )

        for facility_idx in range(num_facilities):
            for node_idx in range(num_nodes):
                dist[facility_idx, node_idx] = node_to_facility_distance(
                    facility_idx,
                    node_idx,
                )

        feasible = np.where(all_facility_sizes <= size_budget_mb)[0]

        selected: set[int] = set()

        best_dist = np.full(
            num_nodes,
            np.inf,
            dtype=float,
        )

        current_total_size = 0.0
        current_cost = np.inf

        while True:
            best_facility = None
            best_new_cost = current_cost

            remaining = [
                int(f)
                for f in feasible
                if int(f) not in selected
                and current_total_size + all_facility_sizes[int(f)] <= size_budget_mb
            ]

            if not remaining:
                break

            # ----------------------------------------------------------
            # Exact vectorized evaluation from the supplied algorithm.
            # ----------------------------------------------------------
            cand_dists = dist[remaining]

            new_best_dists = np.minimum(
                best_dist,
                cand_dists,
            )

            new_costs = (new_best_dists * node_occurrences).sum(axis=1)

            idx = int(np.argmin(new_costs))

            candidate_facility = remaining[idx]
            candidate_cost = float(new_costs[idx])

            # For first facility, accept even if current_cost is inf.
            if np.isinf(current_cost) or candidate_cost < current_cost:
                best_facility = candidate_facility
                best_new_cost = candidate_cost

            if best_facility is None:
                break

            selected.add(best_facility)

            current_total_size += all_facility_sizes[best_facility]

            best_dist = np.minimum(
                best_dist,
                dist[best_facility],
            )

            current_cost = best_new_cost

        selected_packages: set[str] = set()

        for facility_idx in selected:
            selected_packages.update(unique_requests[facility_idx])

        return frozenset(selected_packages)

    def _describe(
        self,
        *,
        selected: frozenset[str],
        requests: Sequence[Request],
        metadata: Metadata,
    ) -> CheckpointPlan:
        """Convert the selected facility union to the old output type.

        ``size_mb`` and ``seconds_saved`` are scored over ``selected`` -- the
        full closure -- because that is what is actually resident once the
        executor imports it and what a checkpoint's disk footprint really is.
        ``imports`` itself is narrower: only the names some request actually
        asked for, not every dependency ``selected`` pulled in to cost them
        correctly. The executor never needs the difference spelled out --
        `import pandas` imports numpy as a side effect regardless of whether
        numpy is separately named -- so sending the full closure there would
        only be redundant. (The catalogue still needs the closure for its own
        pricing, and re-derives it from this trimmed list; see
        ``catalogue.py``.)
        """
        closures = self._closures(requests, metadata)
        served = 0
        saved = 0.0
        requested: set[str] = set()

        for request in requests:
            overlap = selected.intersection(closures[request.top_level_imports])

            if overlap:
                served += 1
                saved += sum(self._import_time(package, metadata) for package in overlap)

            requested.update(request.top_level_imports)

        return CheckpointPlan(
            imports=tuple(sorted(selected.intersection(requested))),
            requests_served=served,
            seconds_saved=saved,
            size_mb=sum(self._size_mb(package, metadata) for package in selected),
        )
