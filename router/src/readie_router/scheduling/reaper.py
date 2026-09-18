"""Bounded state.

The previous router never deleted anything: STATUS_REMOVED was stored as a
value and acted on nowhere, so workers, containers and sessions accumulated for
the process's lifetime.

TTLs are a promise about a well-behaved client. Sessions carry no TTL or cap
of their own here: ClusterState.remove_executor deletes a session outright
the moment the container backing it is gone, so a session can never outlive
its container regardless of how a client behaves.
"""

from __future__ import annotations

from dataclasses import dataclass

from readie_router.clock import Clock
from readie_router.scheduling.models import STATUS_ERROR, Outcome
from readie_router.scheduling.scheduler import Scheduler
from readie_router.scheduling.state import ClusterState


@dataclass(frozen=True, slots=True)
class ReaperPolicy:
    """How long each kind of record may go unreferenced."""

    worker_ttl: float
    executor_ttl: float
    executor_error_ttl: float
    lease_ttl: float


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one sweep removed. Returned so the caller can log it."""

    workers: int = 0
    executors: int = 0
    leases: int = 0

    @property
    def is_empty(self) -> bool:
        """Whether the sweep removed nothing, so it need not be logged."""
        return not (self.workers or self.executors or self.leases)


class Reaper:
    """Removes state nothing refers to any more.

    ``sweep`` is synchronous and therefore atomic on the event loop: the whole
    pass observes one consistent view rather than interleaving with request
    handling.
    """

    def __init__(
        self,
        state: ClusterState,
        scheduler: Scheduler,
        clock: Clock,
        policy: ReaperPolicy,
    ) -> None:
        self._state = state
        self._scheduler = scheduler
        self._clock = clock
        self._policy = policy

    def sweep(self) -> SweepResult:
        """Run one pass and report what it removed."""
        now = self._clock.now()
        return SweepResult(
            leases=self._sweep_leases(now),
            workers=self._sweep_workers(now),
            executors=self._sweep_executors(now),
        )

    def _sweep_leases(self, now: float) -> int:
        """Force-release placements that outlived any plausible execution.

        This should never fire: every handler releases from a ``finally``. It
        exists so that a handler killed in a way that skips its cleanup leaks a
        reservation for minutes rather than forever.
        """
        removed = 0
        for lease in self._state.leases():
            if now - lease.opened_at > self._policy.lease_ttl:
                self._scheduler.release(lease.request_id, Outcome.FAILURE)
                removed += 1
        return removed

    def _sweep_workers(self, now: float) -> int:
        """Evict workers that have gone quiet.

        A backstop behind the prober, which is the primary liveness signal.
        """
        removed = 0
        for worker in self._state.workers():
            if now - worker.last_seen > self._policy.worker_ttl and self._state.evict_worker(
                worker.worker_id
            ):
                removed += 1
        return removed

    def _sweep_executors(self, now: float) -> int:
        """Drop containers the worker never told us about again.

        Reclaims warm containers whose removal notification was lost, and
        clears failed ones once they have served their diagnostic purpose.
        """
        removed = 0
        for worker in self._state.workers():
            for executor in list(worker.executors.values()):
                age = now - executor.last_seen
                if executor.status == STATUS_ERROR:
                    if age > self._policy.executor_error_ttl:
                        self._state.remove_executor(worker.worker_id, executor.container_id)
                        removed += 1
                    continue

                session = self._state.session(executor.session_id)
                busy = session is not None and not session.is_idle
                if age > self._policy.executor_ttl and not busy:
                    self._state.remove_executor(worker.worker_id, executor.container_id)
                    removed += 1
        return removed
