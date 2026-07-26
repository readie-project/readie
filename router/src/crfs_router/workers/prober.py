"""Worker liveness.

Liveness must be *probed*, not inferred from silence. Registry traffic only
flows while an execution is running, so an idle-but-healthy worker sends
nothing and a last-seen TTL alone would evict it. The worker already serves
``grpc.health.v1``, so this needs no change on that side.
"""

from __future__ import annotations

import asyncio

import structlog

from crfs_router.clock import Clock
from crfs_router.scheduling.state import ClusterState
from crfs_router.workers.ports import HealthClient


class WorkerProber:
    """Periodically checks every known worker and evicts the dead ones."""

    def __init__(
        self,
        state: ClusterState,
        health: HealthClient,
        clock: Clock,
        *,
        interval: float,
        timeout: float,
        failure_threshold: int,
    ) -> None:
        self._state = state
        self._health = health
        self._clock = clock
        self._interval = interval
        self._timeout = timeout
        self._failure_threshold = failure_threshold
        self._log = structlog.get_logger("workers.prober")

    async def run(self) -> None:
        """Probe on a fixed interval until cancelled."""
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self.probe_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning("liveness sweep failed", err=str(exc))

    async def probe_once(self) -> None:
        """Probe every worker concurrently.

        Concurrently, and never serially: one hung worker must not delay the
        detection of any other.
        """
        targets = [(w.worker_id, w.worker_uri) for w in self._state.workers() if w.worker_uri]
        if not targets:
            return

        results = await asyncio.gather(
            *(self._health.check(uri, timeout=self._timeout) for _, uri in targets),
            return_exceptions=True,
        )

        now = self._clock.now()
        for (worker_id, worker_uri), result in zip(targets, results, strict=True):
            ok = result is True
            # record_probe_result re-checks that this worker still exists and
            # still advertises this URI. We awaited since reading them, so we
            # must not write back what we read.
            failures = self._state.record_probe_result(worker_id, worker_uri, ok, now)
            if failures >= self._failure_threshold and self._state.evict_worker(worker_id):
                self._log.warning(
                    "evicting unresponsive worker",
                    worker_id=worker_id,
                    worker_uri=worker_uri,
                    failures=failures,
                )
