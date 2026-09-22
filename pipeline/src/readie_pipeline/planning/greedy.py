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
        """Greedily select facilities and hand back each as its own checkpoint.

        ``budget.size_mb`` bounds the total size across every checkpoint
        selected, not each one individually. ``budget.max_checkpoints`` then
        keeps only the first that many: the algorithm selects facilities in
        decreasing marginal value, so its prefix is already the highest-value
        subset.
        """
        requests = corpus.requests
        if not requests:
            return []

        eligible_packages = self._eligible_packages(requests, metadata)
        if not eligible_packages:
            return []

        closures = self._closures(requests, metadata)
        requested: set[str] = set()
        for request in requests:
            requested.update(request.top_level_imports)

        # A facility with no overlap with `requested` is nothing but shared
        # dependencies nobody ever asks for directly: the executor is told to
        # preimport `imports`, not the full closure, so such a checkpoint
        # would ship a real size/build cost for zero real preimport benefit.
        # Dropped before the max_checkpoints cut, so a genuinely useful
        # facility can take the freed slot instead.
        facilities = [
            facility
            for facility in self._select_checkpoints(
                requests=requests,
                eligible_packages=eligible_packages,
                metadata=metadata,
                size_budget_mb=budget.size_mb,
            )
            if facility & requested
        ][: budget.max_checkpoints]

        facility_sizes = [
            sum(self._size_mb(name, metadata) for name in facility) for facility in facilities
        ]

        # A request restores from exactly one checkpoint at serving time --
        # whichever of the captured ones is cheapest for it -- so its overlap
        # is credited to that single checkpoint only, never split across
        # every checkpoint it happens to intersect.
        assignments: list[list[Request]] = [[] for _ in facilities]
        total_latency = 0.0
        remaining_latency = 0.0

        for request in requests:
            closure = closures[request.top_level_imports]
            baseline = self._cost(closure, frozenset(), 0.0, metadata)
            total_latency += baseline

            best_index = None
            best_cost = float("inf")

            for index, (facility, size_mb) in enumerate(
                zip(facilities, facility_sizes, strict=True)
            ):
                if closure.isdisjoint(facility):
                    continue

                cost = self._cost(closure, facility, size_mb, metadata)
                if cost < best_cost:
                    best_index, best_cost = index, cost

            remaining_latency += min(best_cost, baseline)
            if best_index is not None:
                assignments[best_index].append(request)

        plans = [
            self._describe(
                selected=facility,
                assigned=assigned,
                closures=closures,
                requested=requested,
                metadata=metadata,
            )
            for facility, assigned in zip(facilities, assignments, strict=True)
        ]

        print(f"[*] total latency with no checkpoints: {total_latency:.0f}s")
        print(
            f"[*] total latency remaining across {len(plans)} checkpoints: {remaining_latency:.0f}s"
        )

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
        """Apply the old planner's minimum package size.

        Resident memory, not disk footprint: gVisor's checkpoint/restore
        copies back whatever is resident when a sandbox is captured, and a
        package's disk and memory footprints are not proportional to each
        other -- see ``Metadata.memory_size_mb``.
        """
        return max(
            metadata.memory_size_mb(name),
            MIN_SIZE_MB,
        )

    def _cost(
        self,
        closure: frozenset[str],
        packages: frozenset[str],
        size_mb: float,
        metadata: Metadata,
    ) -> float:
        """What a request pays restoring from a checkpoint holding ``packages``.

        The same formula ``_select_checkpoints`` picks facilities by: the
        checkpoint's own restore overhead, plus the import time for whatever
        the request needs that the checkpoint does not already hold resident.
        ``packages=frozenset()`` and ``size_mb=0.0`` is the no-checkpoint case.
        """
        residual = [name for name in closure if name not in STDLIB and name not in packages]
        return self._alpha * size_mb + sum(self._import_time(name, metadata) for name in residual)

    def _select_checkpoints(
        self,
        *,
        requests: Sequence[Request],
        eligible_packages: set[str],
        metadata: Metadata,
        size_budget_mb: float,
    ) -> list[frozenset[str]]:
        """Greedy k-median facility selection: each pick is a checkpoint.

        Facilities and nodes are both the corpus's distinct (eligible-only)
        import closures; a facility is picked when adding it lowers every
        node's distance to its nearest picked facility so far more than any
        other candidate would, until nothing remaining improves on that or
        the shared size budget is spent. Ties -- including which facility is
        picked first, when every node's distance is still infinite -- break on
        ``unique_requests``' sort order, so the result is deterministic.
        """
        closures = self._closures(requests, metadata)
        normalized_requests: list[frozenset[str]] = []

        for request in requests:
            imports = frozenset(
                name for name in closures[request.top_level_imports] if name in eligible_packages
            )

            if imports:
                normalized_requests.append(imports)

        if not normalized_requests:
            return []

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

            return self._alpha * all_facility_sizes[facility_idx] + sum(
                self._import_time(package, metadata) for package in residual
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

        selected: list[int] = []
        selected_set: set[int] = set()

        best_dist = np.full(
            num_nodes,
            np.inf,
            dtype=float,
        )

        current_total_size = 0.0
        current_cost = np.inf

        while True:
            remaining = [
                int(f)
                for f in feasible
                if int(f) not in selected_set
                and current_total_size + all_facility_sizes[int(f)] <= size_budget_mb
            ]

            if not remaining:
                break

            cand_dists = dist[remaining]

            new_best_dists = np.minimum(
                best_dist,
                cand_dists,
            )

            new_costs = (new_best_dists * node_occurrences).sum(axis=1)

            idx = int(np.argmin(new_costs))

            candidate_facility = remaining[idx]
            candidate_cost = float(new_costs[idx])

            # The first facility is always accepted -- there is no baseline
            # yet to weigh it against.
            if np.isinf(current_cost) or candidate_cost < current_cost:
                best_facility, best_new_cost = candidate_facility, candidate_cost
            else:
                best_facility, best_new_cost = None, current_cost

            if best_facility is None:
                break

            selected.append(best_facility)
            selected_set.add(best_facility)

            current_total_size += all_facility_sizes[best_facility]

            best_dist = np.minimum(
                best_dist,
                dist[best_facility],
            )

            current_cost = best_new_cost

        return [unique_requests[facility_idx] for facility_idx in selected]

    def _describe(
        self,
        *,
        selected: frozenset[str],
        assigned: Sequence[Request],
        closures: dict[frozenset[str], frozenset[str]],
        requested: set[str],
        metadata: Metadata,
    ) -> CheckpointPlan:
        """Convert one selected facility to the old output type.

        ``requests_served``/``seconds_saved`` are scored only over
        ``assigned`` -- the requests ``plan()`` decided land on this
        checkpoint specifically, its single cheapest option among the ones
        captured -- because a request restores from exactly one checkpoint at
        serving time, never every one it happens to overlap with. ``size_mb``
        is still scored over the full ``selected`` closure regardless of
        assignment, since that is what is actually resident once the executor
        imports it and what a checkpoint costs to restore.
        ``imports`` itself is narrower still: only the names some request
        *anywhere in the corpus* actually asked for, not every dependency
        ``selected`` pulled in to cost it correctly. The executor never needs
        the difference spelled out -- `import pandas` imports numpy as a side
        effect regardless of whether numpy is separately named -- so sending
        the full closure there would only be redundant. (The catalogue still
        needs the closure for its own pricing, and re-derives it from this
        trimmed list; see ``catalogue.py``.)
        """
        saved = sum(
            self._import_time(package, metadata)
            for request in assigned
            for package in selected.intersection(closures[request.top_level_imports])
        )

        return CheckpointPlan(
            imports=tuple(sorted(selected.intersection(requested))),
            requests_served=len(assigned),
            seconds_saved=saved,
            size_mb=sum(self._size_mb(package, metadata) for package in selected),
        )
