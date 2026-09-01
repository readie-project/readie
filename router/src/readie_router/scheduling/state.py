"""The cluster registry: every worker, container, session and open placement.

**No method here is ``async`` and none of them awaits.** On a single-threaded
event loop that makes every one of them atomic by construction, which is what
lets the scheduler read-modify-write without locks. A coroutine slipped in here
would silently reintroduce the race it was written to prevent, so a guard test
enforces the rule.
"""

from __future__ import annotations

from collections.abc import Iterator

from readie_router.scheduling.models import (
    STATUS_ERROR,
    STATUS_READY,
    STATUS_REMOVED,
    Affinity,
    ExecutorRecord,
    Placement,
    SessionRecord,
    WorkerRecord,
)


class ClusterState:
    """Mutable cluster state.

    Every mutation is create-on-write, matching how workers report: a status
    update for an unknown worker registers it rather than being dropped.
    """

    def __init__(self) -> None:
        self._workers: dict[str, WorkerRecord] = {}
        self._sessions: dict[str, SessionRecord] = {}
        self._leases: dict[str, Placement] = {}
        self._next_lease_id = 1
        #: URIs whose channel should be closed. Drained by the application,
        #: because closing a channel is a coroutine and this layer never awaits.
        self._evicted_uris: list[str] = []

    # -- Identity ---------------------------------------------------------
    def next_lease_id(self) -> int:
        """Return a monotonically increasing lease number."""
        lease_id = self._next_lease_id
        self._next_lease_id += 1
        return lease_id

    # -- Workers ----------------------------------------------------------
    def workers(self) -> Iterator[WorkerRecord]:
        """Iterate every known worker."""
        return iter(list(self._workers.values()))

    def worker(self, worker_id: str) -> WorkerRecord | None:
        """Return a worker, or None."""
        return self._workers.get(worker_id)

    def ensure_worker(self, worker_id: str, now: float) -> WorkerRecord:
        """Return a worker, registering it if this is the first we have heard."""
        worker = self._workers.get(worker_id)
        if worker is None:
            worker = WorkerRecord(worker_id=worker_id, last_seen=now)
            self._workers[worker_id] = worker
        else:
            worker.last_seen = now
        return worker

    def evict_worker(self, worker_id: str) -> bool:
        """Remove a worker and every reference to it.

        In-flight streams are deliberately *not* cancelled. The worker
        deregisters before it drains, so those executions are still running and
        will finish or fail on their own; eviction only stops new work being
        routed here.
        """
        worker = self._workers.pop(worker_id, None)
        if worker is None:
            return False

        for session in self._sessions.values():
            if session.affinity is not None and session.affinity.worker_id == worker_id:
                session.affinity = None

        if worker.worker_uri:
            self._evicted_uris.append(worker.worker_uri)
        return True

    def drain_evicted_uris(self) -> list[str]:
        """Return and clear the URIs whose channels should be closed."""
        drained, self._evicted_uris = self._evicted_uris, []
        return drained

    # -- Executors --------------------------------------------------------
    def ensure_executor(self, worker_id: str, container_id: str, now: float) -> ExecutorRecord:
        """Return a container record, creating it if unknown."""
        worker = self.ensure_worker(worker_id, now)
        executor = worker.executors.get(container_id)
        if executor is None:
            executor = ExecutorRecord(container_id=container_id, worker_id=worker_id, last_seen=now)
            worker.executors[container_id] = executor
        else:
            executor.last_seen = now
        return executor

    def remove_executor(self, worker_id: str, container_id: str) -> None:
        """Delete a container and clear any session pinned to it."""
        worker = self._workers.get(worker_id)
        if worker is not None:
            worker.executors.pop(container_id, None)
        self.clear_affinity_to(worker_id, container_id)

    def clear_affinity_to(self, worker_id: str, container_id: str) -> None:
        """Unpin every session bound to a container.

        Called when the container is gone or broken. A session left pinned to
        it would get NOT_FOUND from the worker on its next request instead of
        quietly cold-starting.
        """
        for session in self._sessions.values():
            affinity = session.affinity
            if (
                affinity is not None
                and affinity.worker_id == worker_id
                and affinity.container_id == container_id
            ):
                session.affinity = None

    # -- Sessions ---------------------------------------------------------
    def session(self, session_id: str) -> SessionRecord | None:
        """Return a session, or None."""
        return self._sessions.get(session_id)

    def sessions(self) -> Iterator[SessionRecord]:
        """Iterate every known session."""
        return iter(list(self._sessions.values()))

    def touch_session(self, session_id: str, now: float) -> SessionRecord:
        """Return a session, creating it if unknown, and mark it as active."""
        session = self._sessions.get(session_id)
        if session is None:
            session = SessionRecord(session_id=session_id, last_seen=now)
            self._sessions[session_id] = session
        else:
            session.last_seen = now
        return session

    def remove_session(self, session_id: str) -> None:
        """Delete a session."""
        self._sessions.pop(session_id, None)

    def session_count(self) -> int:
        """Return how many sessions are tracked."""
        return len(self._sessions)

    # -- Leases -----------------------------------------------------------
    def open_lease(self, placement: Placement) -> None:
        """Record a placement as in flight."""
        self._leases[placement.request_id] = placement

    def lease(self, request_id: str) -> Placement | None:
        """Return an open placement, or None."""
        return self._leases.get(request_id)

    def leases(self) -> Iterator[Placement]:
        """Iterate every open placement."""
        return iter(list(self._leases.values()))

    def close_lease(self, request_id: str) -> Placement | None:
        """Remove a placement, returning it if it was open."""
        return self._leases.pop(request_id, None)

    # -- Reporting from workers -------------------------------------------
    def apply_worker_status(
        self,
        worker_id: str,
        worker_uri: str,
        status: int,
        now: float,
        *,
        mem_total: int = 0,
        max_executors: int = 0,
        flavor: str = "",
        gpu_mem_total: int = 0,
    ) -> None:
        """Record a worker's lifecycle state.

        STATUS_REMOVED evicts. That is the graceful path: the worker sends it
        before it drains, so the router stops routing there while its in-flight
        work finishes.
        """
        if status == STATUS_REMOVED:
            self.evict_worker(worker_id)
            return

        worker = self.ensure_worker(worker_id, now)
        worker.worker_uri = worker_uri or worker.worker_uri
        worker.status = status
        worker.probe_failures = 0
        if mem_total:
            worker.mem_total = mem_total
        if max_executors:
            worker.max_executors = max_executors
        if flavor:
            worker.flavor = flavor
        if gpu_mem_total:
            worker.gpu_mem_total = gpu_mem_total

    def apply_executor_status(
        self,
        worker_id: str,
        container_id: str,
        *,
        session_id: str,
        request_id: str,
        status: int,
        now: float,
    ) -> None:
        """Record a container's lifecycle state.

        This is also the *reliable* affinity channel. The proxy stream only
        reveals a container id if the execution produces output, so a silent
        execution would otherwise never bind its session.
        """
        if status == STATUS_REMOVED:
            self.remove_executor(worker_id, container_id)
            return

        executor = self.ensure_executor(worker_id, container_id, now)
        executor.status = status
        if session_id:
            executor.session_id = session_id

        if status == STATUS_ERROR:
            # Keep the record for observability, but stop routing to it.
            self.clear_affinity_to(worker_id, container_id)
            return

        if session_id and request_id:
            self.bind_session(request_id, worker_id, container_id, now=now)

    def apply_worker_utilization(
        self,
        worker_id: str,
        now: float,
        *,
        cpu_util: int,
        cpu_total: int,
        gpu_util: int,
        gpu_total: int,
        mem_used: int = 0,
        mem_total: int = 0,
        executor_count: int = 0,
        gpu_mem_used: int = 0,
        gpu_mem_total: int = 0,
    ) -> None:
        """Record a worker's load.

        The previous implementation wrote these onto an *executor* keyed by a
        field the message does not have, raising AttributeError on every call
        and leaving worker load permanently zero.
        """
        worker = self.ensure_worker(worker_id, now)
        worker.cpu_util = cpu_util
        worker.cpu_total = cpu_total
        worker.gpu_util = gpu_util
        worker.gpu_total = gpu_total
        if mem_total:
            worker.mem_total = mem_total
        worker.mem_used = mem_used
        worker.executor_count = executor_count
        worker.gpu_mem_used = gpu_mem_used
        if gpu_mem_total:
            worker.gpu_mem_total = gpu_mem_total

    def apply_executor_utilization(
        self,
        worker_id: str,
        container_id: str,
        now: float,
        *,
        cpu_util: int,
        cpu_total: int,
        gpu_util: int,
        gpu_total: int,
    ) -> None:
        """Record one container's load."""
        executor = self.ensure_executor(worker_id, container_id, now)
        executor.cpu_util = cpu_util
        executor.cpu_total = cpu_total
        executor.gpu_util = gpu_util
        executor.gpu_total = gpu_total

    # -- Reconciliation ---------------------------------------------------
    def bind_session(
        self,
        request_id: str,
        worker_id: str,
        container_id: str,
        *,
        now: float,
        checkpoint_id: str | None = None,
    ) -> None:
        """Record the container a placement actually landed on.

        Idempotent and monotonic. Two sources race to call this — the worker's
        ExecutorStatus(BUSY) and the first response on the proxy stream — so
        whichever arrives first wins and the second is a no-op. A stale lease
        can never overwrite a newer session binding.
        """
        if not container_id:
            return

        lease = self._leases.get(request_id)
        if lease is None:
            return  # already released; nothing to bind

        session = self._sessions.get(lease.session_id)
        if session is None:
            return

        current = session.affinity
        if current is not None and current.seq > lease.lease_id:
            return  # a newer placement owns this session

        session.affinity = Affinity(
            worker_id=worker_id, container_id=container_id, seq=lease.lease_id
        )

        executor = self.ensure_executor(worker_id, container_id, now)
        executor.session_id = lease.session_id
        if checkpoint_id is not None:
            executor.checkpoint_id = checkpoint_id
        if executor.status not in (STATUS_READY, STATUS_ERROR):
            executor.status = STATUS_READY

    def record_probe_result(self, worker_id: str, worker_uri: str, ok: bool, now: float) -> int:
        """Record a liveness probe.

        Re-checks that the worker still exists and still advertises the URI
        that was probed. The prober awaits between reading state and calling
        this, so it must never write back what it read.
        """
        worker = self._workers.get(worker_id)
        if worker is None or worker.worker_uri != worker_uri:
            return 0

        if ok:
            worker.probe_failures = 0
            worker.last_seen = now
            return 0

        worker.probe_failures += 1
        return worker.probe_failures
