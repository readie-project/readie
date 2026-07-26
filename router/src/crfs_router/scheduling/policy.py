"""Worker selection.

Filters decide who *may* take a request; the scorer decides who *should*. They
are separate objects rather than subclasses of a base selector, so a new policy
is a new composition and each filter is independently testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from crfs_router.scheduling.models import Demand, WorkerView


class WorkerFilter(Protocol):
    """Decides whether a worker may take a request at all."""

    def admits(self, worker: WorkerView, demand: Demand) -> bool:
        """Return whether this worker is eligible."""
        ...


class WorkerScorer(Protocol):
    """Ranks eligible workers. Lower is better."""

    def score(self, worker: WorkerView, demand: Demand) -> tuple[float, ...]:
        """Return a sort key; the lowest wins."""
        ...


class WorkerSelector(Protocol):
    """Chooses where to place a request."""

    def select(self, candidates: Sequence[WorkerView], demand: Demand) -> str | None:
        """Return a worker id, or None if nothing can take the work."""
        ...


@dataclass(frozen=True, slots=True)
class MemoryHeadroomFilter:
    """Rejects a worker that cannot fit the request's allocation.

    Counts memory already committed to placements that have not reported back
    yet, not just what the worker last reported: two requests arriving between
    utilisation samples would otherwise both see an idle worker.

    Inert when the worker has not reported a total, which is the state until
    the worker starts sending capacity.
    """

    headroom: float = 0.9

    def admits(self, worker: WorkerView, demand: Demand) -> bool:
        """Return whether the allocation fits within the configured headroom."""
        if worker.mem_total <= 0:
            return True
        committed = worker.mem_used + worker.reserved_bytes + demand.cpu_alloc
        return committed <= worker.mem_total * self.headroom


@dataclass(frozen=True, slots=True)
class ExecutorCountFilter:
    """Rejects a worker already holding its maximum number of containers.

    Inert when the worker advertises no limit.
    """

    def admits(self, worker: WorkerView, demand: Demand) -> bool:  # noqa: ARG002
        """Return whether the worker has room for another container."""
        if worker.max_executors <= 0:
            return True
        return worker.executor_count + worker.inflight <= worker.max_executors


@dataclass(frozen=True, slots=True)
class LeastLoadedScorer:
    """Prefers the least loaded worker, on the best signal available.

    The terms are ordered by how directly they measure what placement actually
    consumes. Memory is first because ``cpu_alloc`` is a memory budget; CPU is
    a weaker proxy; in-flight count is always available and is the only signal
    that exists before a worker reports capacity.

    Every term is present in the key so the later ones break ties, and the
    worker id is last so the result is deterministic — a single-worker or
    freshly started cluster produces reproducible output, and there is no
    randomness to make a test flaky.
    """

    def score(self, worker: WorkerView, demand: Demand) -> tuple[float, ...]:  # noqa: ARG002
        """Return the sort key for a worker."""
        memory_pressure = (
            (worker.mem_used + worker.reserved_bytes) / worker.mem_total
            if worker.mem_total > 0
            else 0.0
        )
        cpu_pressure = worker.cpu_util / worker.cpu_total if worker.cpu_total > 0 else 0.0
        return (memory_pressure, cpu_pressure, float(worker.inflight))


@dataclass(frozen=True, slots=True)
class CompositeSelector:
    """Applies every filter, then picks the best-scoring survivor."""

    filters: tuple[WorkerFilter, ...]
    scorer: WorkerScorer

    def select(self, candidates: Sequence[WorkerView], demand: Demand) -> str | None:
        """Return the chosen worker id, or None when nothing is eligible."""
        eligible = [
            worker for worker in candidates if all(f.admits(worker, demand) for f in self.filters)
        ]
        if not eligible:
            return None

        best = min(eligible, key=lambda w: (*self.scorer.score(w, demand), w.worker_id))
        return best.worker_id


def default_selector(*, memory_headroom: float = 0.9) -> CompositeSelector:
    """Build the selector the router uses."""
    return CompositeSelector(
        filters=(MemoryHeadroomFilter(headroom=memory_headroom), ExecutorCountFilter()),
        scorer=LeastLoadedScorer(),
    )
