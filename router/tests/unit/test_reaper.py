"""Bounded state.

The previous router deleted nothing: STATUS_REMOVED was stored and acted on
nowhere, so workers, containers and sessions accumulated for the life of the
process.
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
    session_ttl=1800.0,
    lease_ttl=7200.0,
    max_sessions=3,
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


def test_an_idle_session_is_deleted(state: ClusterState, clock: FakeClock, reaper: Reaper) -> None:
    """This is the fix for unbounded growth."""
    state.touch_session("sess-1", clock.now())

    clock.advance(1801.0)
    assert reaper.sweep().sessions == 1
    assert state.session("sess-1") is None


def test_a_session_with_work_in_flight_is_kept(
    state: ClusterState, clock: FakeClock, scheduler: Scheduler, reaper: Reaper
) -> None:
    state.apply_worker_status("w1", "w1:50052", STATUS_READY, clock.now())
    scheduler.provision(ProvisionRequest(request_id="req-1", session_id="sess-1", demand=DEMAND))

    clock.advance(1801.0)
    reaper.sweep()

    assert state.session("sess-1") is not None, "a running request must not lose its session"


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


def test_the_session_cap_evicts_the_least_recently_used(
    state: ClusterState, clock: FakeClock, reaper: Reaper
) -> None:
    """A TTL is a promise about a well-behaved client; the cap is about memory."""
    for i in range(5):
        state.touch_session(f"sess-{i}", clock.now())
        clock.advance(1.0)

    result = reaper.sweep()

    assert result.evicted_by_cap == 2
    assert state.session_count() == POLICY.max_sessions
    assert state.session("sess-0") is None, "the oldest must go first"
    assert state.session("sess-4") is not None


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
