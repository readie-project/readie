"""Bounded state.

The previous router deleted nothing: STATUS_REMOVED was stored and acted on
nowhere, so workers, containers and sessions accumulated for the life of the
process. Sessions are still bounded, but not by this reaper: a session is
removed the instant the container backing it is gone (ClusterState.
remove_executor), never on a timer or a cap of its own - see test_scheduler.py
for that behaviour.
"""

from __future__ import annotations

import pytest

from readie_router.clock import FakeClock
from readie_router.scheduling.models import (
    RESOURCE_MEMORY,
    STATUS_ERROR,
    STATUS_READY,
    Budget,
    Demand,
    Outcome,
)
from readie_router.scheduling.policy import default_selector
from readie_router.scheduling.reaper import Reaper, ReaperPolicy
from readie_router.scheduling.scheduler import ProvisionRequest, Scheduler
from readie_router.scheduling.state import ClusterState

DEMAND = Demand(budgets=(Budget(kind=RESOURCE_MEMORY, alloc=512 * 1024 * 1024),))

POLICY = ReaperPolicy(
    worker_ttl=30.0,
    executor_ttl=600.0,
    executor_error_ttl=60.0,
    lease_ttl=7200.0,
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def state() -> ClusterState:
    return ClusterState()


@pytest.fixture
def scheduler(state: ClusterState, clock: FakeClock) -> Scheduler:
    return Scheduler(
        state=state,
        selector=default_selector(),
        clock=clock,
    )


@pytest.fixture
def reaper(state: ClusterState, scheduler: Scheduler, clock: FakeClock) -> Reaper:
    return Reaper(state=state, scheduler=scheduler, clock=clock, policy=POLICY)


def test_a_silent_worker_is_evicted(state: ClusterState, clock: FakeClock, reaper: Reaper) -> None:
    state.apply_worker_status("w1", "w1:50052", STATUS_READY, clock.now())

    clock.advance(31.0)
    result = reaper.sweep()

    assert result.workers == 1
    assert state.worker("w1") is None


def test_a_recently_seen_worker_survives(
    state: ClusterState, clock: FakeClock, reaper: Reaper
) -> None:
    state.apply_worker_status("w1", "w1:50052", STATUS_READY, clock.now())

    clock.advance(10.0)
    assert reaper.sweep().workers == 0
    assert state.worker("w1") is not None


def test_a_failed_container_is_reclaimed_sooner_than_a_healthy_one(
    state: ClusterState, clock: FakeClock, reaper: Reaper
) -> None:
    """A broken container is kept briefly for diagnosis, then dropped."""
    state.apply_worker_status("w1", "w1:50052", STATUS_READY, clock.now())
    executor = state.ensure_executor("w1", "c1", clock.now())
    executor.status = STATUS_ERROR

    clock.advance(61.0)
    # Keep the worker itself alive; otherwise evicting it would remove the
    # container as a side effect and this would not test the error TTL.
    state.apply_worker_status("w1", "w1:50052", STATUS_READY, clock.now())

    assert reaper.sweep().executors == 1

    worker = state.worker("w1")
    assert worker is not None
    assert "c1" not in worker.executors


def test_a_leaked_lease_is_force_released(
    state: ClusterState, clock: FakeClock, scheduler: Scheduler, reaper: Reaper
) -> None:
    """Should never fire: every handler releases from a finally.

    It exists so a handler killed in a way that skips its cleanup leaks a
    reservation for minutes rather than for the life of the process.
    """
    state.apply_worker_status("w1", "w1:50052", STATUS_READY, clock.now())
    scheduler.provision(ProvisionRequest(request_id="req-1", session_id="sess-1", demand=DEMAND))

    worker = state.worker("w1")
    assert worker is not None
    assert worker.inflight == 1

    clock.advance(7201.0)
    assert reaper.sweep().leases == 1
    assert worker.inflight == 0
    assert worker.reserved_bytes == 0


def test_reclaiming_a_lost_executor_expires_its_session(
    state: ClusterState, clock: FakeClock, scheduler: Scheduler, reaper: Reaper
) -> None:
    """A session's only expiry path is losing the container behind it.

    This is the backstop for a lost STATUS_REMOVED notification, not the
    graceful path (see test_scheduler.py for that), but it must expire the
    session the same way: sessions have no TTL or cap of their own.
    """
    state.apply_worker_status("w1", "w1:50052", STATUS_READY, clock.now())
    placement = scheduler.provision(
        ProvisionRequest(request_id="req-1", session_id="sess-1", demand=DEMAND)
    )
    scheduler.bind("req-1", placement.worker_id, "c1")
    scheduler.release("req-1", Outcome.SUCCESS)

    session = state.session("sess-1")
    assert session is not None
    assert not session.expired

    clock.advance(601.0)
    # Keep the worker itself alive; otherwise evicting it would clear the
    # affinity as a side effect (evict_worker) instead of exercising
    # remove_executor, which is what this test is about.
    state.apply_worker_status("w1", "w1:50052", STATUS_READY, clock.now())

    assert reaper.sweep().executors == 1
    assert session.expired


def test_an_empty_sweep_reports_nothing_to_log(reaper: Reaper) -> None:
    assert reaper.sweep().is_empty


def test_release_after_a_forced_release_is_harmless(
    state: ClusterState, clock: FakeClock, scheduler: Scheduler, reaper: Reaper
) -> None:
    """The handler's finally still runs after the reaper has intervened."""
    state.apply_worker_status("w1", "w1:50052", STATUS_READY, clock.now())
    scheduler.provision(ProvisionRequest(request_id="req-1", session_id="sess-1", demand=DEMAND))

    clock.advance(7201.0)
    reaper.sweep()

    assert scheduler.release("req-1", Outcome.SUCCESS) is None
