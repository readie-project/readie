"""Placement decisions.

Like ``ClusterState``, **nothing here is ``async`` and nothing awaits**. Each
public method is one synchronous transaction, and therefore atomic on a
single-threaded event loop. That property is the whole concurrency design: it is
why ``provision`` can decide and reserve without a lock, and why ``release`` is
safe to call from a handler's ``finally`` without risk of deadlock.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from readie_router.clock import Clock
from readie_router.errors import NoCapacityError, NoWorkersRegisteredError
from readie_router.scheduling.catalogue import Catalogue
from readie_router.scheduling.models import (
    RESOURCE_MEMORY,
    Demand,
    Outcome,
    Placement,
    SessionRecord,
    WorkerView,
)
from readie_router.scheduling.policy import WorkerSelector
from readie_router.scheduling.state import ClusterState


@dataclass(frozen=True, slots=True)
class ProvisionRequest:
    """What the transport knows when it asks for a placement."""

    request_id: str
    session_id: str
    demand: Demand


class Scheduler:
    """Decides where requests run, and keeps the registry honest about it."""

    def __init__(
        self,
        state: ClusterState,
        selector: WorkerSelector,
        clock: Clock,
        catalogues: Mapping[str, Catalogue] | None = None,
    ) -> None:
        self._state = state
        self._selector = selector
        self._clock = clock
        # Per-flavor checkpoint catalogues. Empty means no request-time
        # selection, i.e. cold starts -- the behaviour before catalogues existed.
        self._catalogues = catalogues or {}

    # -- Placement --------------------------------------------------------
    def provision(self, request: ProvisionRequest) -> Placement:
        """Choose a worker and reserve capacity on it, atomically.

        Deciding and reserving cannot be separated. The previous implementation
        only read: two concurrent requests for one session both observed no
        affinity, both cold-started, and whichever response landed last won the
        session. Committing the reservation here means the next caller sees
        this one's load, and the next request for this session sees its
        pending binding.

        Raises:
            NoWorkersRegisteredError: nothing has registered, or nothing is healthy.
            NoCapacityError: workers exist but none can take this allocation.
        """
        now = self._clock.now()
        session = self._state.touch_session(request.session_id, now)
        session.requests.add(request.request_id)

        demand = request.demand
        lease_id = self._state.next_lease_id()

        worker_id, container_id, checkpoint_id, warm = self._resolve_target(session, demand)
        worker = self._state.worker(worker_id)
        if worker is None:  # pragma: no cover - _resolve_target only returns live workers
            raise NoWorkersRegisteredError

        worker.inflight += 1
        # Only memory is reserved: it is the tight resource and the one the
        # cluster reports capacity for. The reservation covers the initial
        # budget; in-flight growth is bounded by the worker's own capacity.
        worker.reserved_bytes += demand.alloc_of(RESOURCE_MEMORY)

        placement = Placement(
            lease_id=lease_id,
            request_id=request.request_id,
            session_id=request.session_id,
            worker_id=worker_id,
            worker_uri=worker.worker_uri,
            container_id=container_id,
            checkpoint_id=checkpoint_id,
            budgets=demand.budgets,
            resources=demand.resources,
            warm=warm,
            opened_at=now,
        )
        self._state.open_lease(placement)

        # Record the intended binding immediately. A concurrent request for the
        # same session must not see an empty affinity just because this one has
        # not reached the worker yet.
        if warm and container_id:
            self._state.bind_session(request.request_id, worker_id, container_id, now=now)

        return placement

    def _resolve_target(self, session: SessionRecord, demand: Demand) -> tuple[str, str, str, bool]:
        """Pick a worker and, when reusing, the container to resume.

        Affinity beats load. A warm container holds the session's live Python
        state, so placing the session elsewhere silently loses it — a much
        worse outcome than an imbalanced cluster.
        """
        affinity = session.affinity
        if affinity is not None:
            worker = self._state.worker(affinity.worker_id)
            if worker is not None and worker.is_selectable:
                executor = worker.executors.get(affinity.container_id)
                if executor is not None and executor.is_reusable:
                    return (
                        affinity.worker_id,
                        affinity.container_id,
                        executor.checkpoint_id,
                        True,
                    )
            # The pinned target is gone. This is user-visible state loss, so it
            # is the caller's job to log it; here we simply fall through.
            session.affinity = None

        candidates = [
            WorkerView.of(worker) for worker in self._state.workers() if worker.is_selectable
        ]
        if not candidates:
            raise NoWorkersRegisteredError

        chosen = self._selector.select(candidates, demand)
        if chosen is None:
            raise NoCapacityError(candidates=len(candidates))

        # A cold start: pick the cheapest checkpoint from the chosen worker's
        # flavor catalogue. A GPU worker serving a CPU request restores a GPU
        # checkpoint, so selection follows the worker, not the request.
        checkpoint_id = self._select_checkpoint(chosen, demand)

        # An empty container id is the worker's "provision a new one" sentinel.
        return chosen, "", checkpoint_id, False

    def _select_checkpoint(self, worker_id: str, demand: Demand) -> str:
        """Choose a checkpoint for a cold start on ``worker_id``, or ``""``."""
        worker = self._state.worker(worker_id)
        if worker is None:  # pragma: no cover - the caller just selected it
            return ""
        catalogue = self._catalogues.get(worker.flavor)
        if catalogue is None:
            return ""
        return catalogue.select(demand.resources)

    # -- Reconciliation ---------------------------------------------------
    def bind(
        self,
        request_id: str,
        worker_id: str,
        container_id: str,
        checkpoint_id: str = "",
    ) -> None:
        """Record where a placement actually landed.

        Called from two racing sources — the worker's ExecutorStatus(BUSY) and
        the first response on the proxy stream — so it must be idempotent.
        """
        self._state.bind_session(
            request_id,
            worker_id,
            container_id,
            now=self._clock.now(),
            checkpoint_id=checkpoint_id,
        )

    def release(self, request_id: str, outcome: Outcome) -> Placement | None:
        """Close a placement and give back its reservation.

        Idempotent: a double release is a no-op, which matters because this is
        called from a ``finally`` that may run after an earlier explicit
        release.
        """
        lease = self._state.close_lease(request_id)
        if lease is None:
            return None

        worker = self._state.worker(lease.worker_id)
        if worker is not None:
            worker.inflight = max(0, worker.inflight - 1)
            worker.reserved_bytes = max(0, worker.reserved_bytes - lease.alloc_of(RESOURCE_MEMORY))

        session = self._state.session(lease.session_id)
        if session is None:
            return lease

        session.requests.discard(request_id)
        session.last_seen = self._clock.now()

        if outcome is Outcome.FAILURE:
            affinity = session.affinity
            if affinity is not None and affinity.seq == lease.lease_id:
                # A failed placement leaves the session pinned to a container
                # that may never have been created. Clearing it stops every
                # later request for this session queueing behind it.
                session.affinity = None

        return lease
