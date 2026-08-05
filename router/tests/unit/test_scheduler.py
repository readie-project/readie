"""The scheduler's state machine.

No event loop and no gRPC: the domain is synchronous by design, which is what
makes these tests fast and deterministic.
"""

from __future__ import annotations

import pytest

from crfs_router.clock import FakeClock
from crfs_router.errors import NoCapacityError, NoWorkersRegisteredError
from crfs_router.scheduling.models import (
    RESOURCE_MEMORY,
    STATUS_BUSY,
    STATUS_ERROR,
    STATUS_READY,
    STATUS_REMOVED,
    Budget,
    Demand,
    Outcome,
    Placement,
)
from crfs_router.scheduling.policy import default_selector
from crfs_router.scheduling.scheduler import ProvisionRequest, Scheduler
from crfs_router.scheduling.state import ClusterState

ALLOC = 512 * 1024 * 1024


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


def register(state: ClusterState, worker_id: str, *, now: float = 0.0, **kwargs: int) -> None:
    """Register a healthy worker, as PostWorkerStatus(READY) would."""
    state.apply_worker_status(worker_id, f"{worker_id}:50052", STATUS_READY, now, **kwargs)


def demand(memory: int = ALLOC) -> Demand:
    """A demand carrying a single memory budget."""
    return Demand(budgets=(Budget(kind=RESOURCE_MEMORY, alloc=memory),))


def provision(scheduler: Scheduler, request_id: str, session_id: str) -> Placement:
    """Place a request carrying the default memory budget."""
    return scheduler.provision(
        ProvisionRequest(request_id=request_id, session_id=session_id, demand=demand())
    )


# ---------------------------------------------------------------------------
# The cold-router crash
# ---------------------------------------------------------------------------


def test_provision_on_a_cold_router_raises_instead_of_crashing(scheduler: Scheduler) -> None:
    """The old provision did self.workers["worker-1"] and raised KeyError."""
    with pytest.raises(NoWorkersRegisteredError):
        provision(scheduler, "req-1", "sess-1")


def test_provision_never_hardcodes_a_worker_id(scheduler: Scheduler, state: ClusterState) -> None:
    """The worker's id is config-driven; the old router assumed "worker-1"."""
    register(state, "some-other-worker")

    placement = provision(scheduler, "req-1", "sess-1")
    assert placement.worker_id == "some-other-worker"
    assert placement.worker_uri == "some-other-worker:50052"


def test_a_registered_but_unhealthy_worker_is_not_selectable(
    scheduler: Scheduler, state: ClusterState
) -> None:
    state.apply_worker_status("w1", "w1:50052", STATUS_ERROR, 0.0)

    with pytest.raises(NoWorkersRegisteredError):
        provision(scheduler, "req-1", "sess-1")


def test_a_worker_that_never_advertised_a_uri_is_not_selectable(
    scheduler: Scheduler, state: ClusterState
) -> None:
    """Dialing an empty URI fails deep inside the transport; reject it here."""
    state.apply_worker_status("w1", "", STATUS_READY, 0.0)

    with pytest.raises(NoWorkersRegisteredError):
        provision(scheduler, "req-1", "sess-1")


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_selection_prefers_the_less_loaded_worker(
    scheduler: Scheduler, state: ClusterState
) -> None:
    register(state, "busy", mem_total=8 << 30)
    register(state, "idle", mem_total=8 << 30)
    state.apply_worker_utilization(
        "busy",
        0.0,
        cpu_util=0,
        cpu_total=0,
        gpu_util=0,
        gpu_total=0,
        mem_used=6 << 30,
        mem_total=8 << 30,
    )
    state.apply_worker_utilization(
        "idle",
        0.0,
        cpu_util=0,
        cpu_total=0,
        gpu_util=0,
        gpu_total=0,
        mem_used=1 << 30,
        mem_total=8 << 30,
    )

    assert provision(scheduler, "req-1", "sess-1").worker_id == "idle"


def test_selection_falls_back_to_inflight_when_no_capacity_is_reported(
    scheduler: Scheduler, state: ClusterState
) -> None:
    """Today's worker reports no worker-level load, so this is the live path."""
    register(state, "w1")
    register(state, "w2")

    first = provision(scheduler, "req-1", "sess-1")
    second = provision(scheduler, "req-2", "sess-2")

    assert {first.worker_id, second.worker_id} == {"w1", "w2"}, (
        "the second request must avoid the worker already holding one"
    )


def test_selection_is_deterministic_on_a_tie(scheduler: Scheduler, state: ClusterState) -> None:
    """No randomness: a tie breaks lexicographically so tests cannot flake."""
    register(state, "b-worker")
    register(state, "a-worker")

    assert provision(scheduler, "req-1", "sess-1").worker_id == "a-worker"


def test_a_full_worker_is_rejected_with_a_distinct_error(
    scheduler: Scheduler, state: ClusterState
) -> None:
    """Distinguish "nothing registered" from "everything is full"."""
    register(state, "w1", mem_total=1 << 30)
    state.apply_worker_utilization(
        "w1",
        0.0,
        cpu_util=0,
        cpu_total=0,
        gpu_util=0,
        gpu_total=0,
        mem_used=1 << 30,
        mem_total=1 << 30,
    )

    with pytest.raises(NoCapacityError):
        provision(scheduler, "req-1", "sess-1")


def test_reservations_are_counted_before_the_worker_reports_back(
    scheduler: Scheduler, state: ClusterState
) -> None:
    """Two requests between utilisation samples must not both see an idle worker."""
    # Room for one allocation inside the 0.9 headroom, but not for two.
    register(state, "w1", mem_total=int(ALLOC * 1.5))

    provision(scheduler, "req-1", "sess-1")
    with pytest.raises(NoCapacityError):
        provision(scheduler, "req-2", "sess-2")


# ---------------------------------------------------------------------------
# Session affinity
# ---------------------------------------------------------------------------


def test_a_new_session_gets_a_cold_container(scheduler: Scheduler, state: ClusterState) -> None:
    register(state, "w1")

    placement = provision(scheduler, "req-1", "sess-1")
    assert placement.container_id == "", "empty means 'provision a new one'"
    assert placement.warm is False


def test_a_session_returns_to_its_warm_container(scheduler: Scheduler, state: ClusterState) -> None:
    register(state, "w1")
    register(state, "w2")

    first = provision(scheduler, "req-1", "sess-1")
    scheduler.bind("req-1", first.worker_id, "container-a")
    state.apply_executor_status(
        "w1", "container-a", session_id="sess-1", request_id="req-1", status=STATUS_READY, now=0.0
    )
    scheduler.release("req-1", Outcome.SUCCESS)

    second = provision(scheduler, "req-2", "sess-1")
    assert second.container_id == "container-a"
    assert second.worker_id == first.worker_id
    assert second.warm is True


def test_affinity_outranks_load(scheduler: Scheduler, state: ClusterState) -> None:
    """A warm container holds the session's Python state; moving loses it."""
    register(state, "w1", mem_total=8 << 30)
    register(state, "w2", mem_total=8 << 30)

    first = provision(scheduler, "req-1", "sess-1")
    scheduler.bind("req-1", first.worker_id, "container-a")
    state.apply_executor_status(
        first.worker_id,
        "container-a",
        session_id="sess-1",
        request_id="req-1",
        status=STATUS_READY,
        now=0.0,
    )
    scheduler.release("req-1", Outcome.SUCCESS)

    # Make the pinned worker look terrible.
    state.apply_worker_utilization(
        first.worker_id,
        0.0,
        cpu_util=0,
        cpu_total=0,
        gpu_util=0,
        gpu_total=0,
        mem_used=7 << 30,
        mem_total=8 << 30,
    )

    assert provision(scheduler, "req-2", "sess-1").worker_id == first.worker_id


def test_a_different_session_does_not_reuse_the_container(
    scheduler: Scheduler, state: ClusterState
) -> None:
    register(state, "w1")

    first = provision(scheduler, "req-1", "sess-1")
    scheduler.bind("req-1", first.worker_id, "container-a")
    scheduler.release("req-1", Outcome.SUCCESS)

    assert provision(scheduler, "req-2", "sess-2").container_id == ""


def test_affinity_is_dropped_when_the_container_is_gone(
    scheduler: Scheduler, state: ClusterState
) -> None:
    register(state, "w1")

    first = provision(scheduler, "req-1", "sess-1")
    scheduler.bind("req-1", first.worker_id, "container-a")
    scheduler.release("req-1", Outcome.SUCCESS)

    state.apply_executor_status(
        "w1", "container-a", session_id="sess-1", request_id="", status=STATUS_REMOVED, now=0.0
    )

    assert provision(scheduler, "req-2", "sess-1").container_id == ""


def test_affinity_is_dropped_when_the_container_failed(
    scheduler: Scheduler, state: ClusterState
) -> None:
    """Resuming a broken interpreter is worse than cold-starting a working one."""
    register(state, "w1")

    first = provision(scheduler, "req-1", "sess-1")
    scheduler.bind("req-1", first.worker_id, "container-a")
    scheduler.release("req-1", Outcome.SUCCESS)

    state.apply_executor_status(
        "w1", "container-a", session_id="sess-1", request_id="", status=STATUS_ERROR, now=0.0
    )

    assert provision(scheduler, "req-2", "sess-1").container_id == ""


def test_affinity_is_dropped_when_the_worker_is_evicted(
    scheduler: Scheduler, state: ClusterState
) -> None:
    register(state, "w1")
    register(state, "w2")

    first = provision(scheduler, "req-1", "sess-1")
    scheduler.bind("req-1", first.worker_id, "container-a")
    scheduler.release("req-1", Outcome.SUCCESS)

    state.evict_worker(first.worker_id)

    second = provision(scheduler, "req-2", "sess-1")
    assert second.worker_id != first.worker_id
    assert second.container_id == ""


# ---------------------------------------------------------------------------
# The provision / reconcile race
# ---------------------------------------------------------------------------


def test_a_second_request_for_a_session_sees_the_first_pending_binding(
    scheduler: Scheduler, state: ClusterState
) -> None:
    """The core race: both requests used to observe no affinity and cold-start."""
    register(state, "w1")

    first = provision(scheduler, "req-1", "sess-1")
    scheduler.bind("req-1", first.worker_id, "container-a")

    # req-2 arrives while req-1 is still open.
    second = provision(scheduler, "req-2", "sess-1")
    assert second.container_id == "container-a"


def test_binding_is_idempotent_across_its_two_racing_sources(
    scheduler: Scheduler, state: ClusterState
) -> None:
    """ExecutorStatus(BUSY) and the first stream response both bind."""
    register(state, "w1")
    provision(scheduler, "req-1", "sess-1")

    state.apply_executor_status(
        "w1", "container-a", session_id="sess-1", request_id="req-1", status=STATUS_BUSY, now=0.0
    )
    scheduler.bind("req-1", "w1", "container-a")

    session = state.session("sess-1")
    assert session is not None
    assert session.affinity is not None
    assert session.affinity.container_id == "container-a"


def test_a_stale_lease_cannot_clobber_a_newer_binding(
    scheduler: Scheduler, state: ClusterState
) -> None:
    """A slow response from an older request must not steal the session."""
    register(state, "w1")

    old = provision(scheduler, "req-old", "sess-1")
    new = provision(scheduler, "req-new", "sess-1")
    assert new.lease_id > old.lease_id

    scheduler.bind("req-new", "w1", "container-new")
    scheduler.bind("req-old", "w1", "container-old")  # arrives late

    session = state.session("sess-1")
    assert session is not None
    assert session.affinity is not None
    assert session.affinity.container_id == "container-new"


def test_binding_an_already_released_request_is_ignored(
    scheduler: Scheduler, state: ClusterState
) -> None:
    register(state, "w1")
    provision(scheduler, "req-1", "sess-1")
    scheduler.release("req-1", Outcome.SUCCESS)

    scheduler.bind("req-1", "w1", "container-a")

    session = state.session("sess-1")
    assert session is not None
    assert session.affinity is None


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def test_release_returns_the_reservation(scheduler: Scheduler, state: ClusterState) -> None:
    register(state, "w1")
    provision(scheduler, "req-1", "sess-1")

    worker = state.worker("w1")
    assert worker is not None
    assert worker.inflight == 1
    assert worker.reserved_bytes == ALLOC

    scheduler.release("req-1", Outcome.SUCCESS)
    assert worker.inflight == 0
    assert worker.reserved_bytes == 0


def test_release_is_idempotent(scheduler: Scheduler, state: ClusterState) -> None:
    """It is called from a finally that may follow an explicit release."""
    register(state, "w1")
    provision(scheduler, "req-1", "sess-1")

    assert scheduler.release("req-1", Outcome.SUCCESS) is not None
    assert scheduler.release("req-1", Outcome.SUCCESS) is None

    worker = state.worker("w1")
    assert worker is not None
    assert worker.inflight == 0


def test_a_failed_placement_clears_the_affinity_it_created(
    scheduler: Scheduler, state: ClusterState
) -> None:
    """Otherwise every later request queues behind a container that never existed."""
    register(state, "w1")

    provision(scheduler, "req-1", "sess-1")
    scheduler.bind("req-1", "w1", "container-a")
    scheduler.release("req-1", Outcome.FAILURE)

    session = state.session("sess-1")
    assert session is not None
    assert session.affinity is None


def test_a_failure_does_not_clear_a_newer_sessions_affinity(
    scheduler: Scheduler, state: ClusterState
) -> None:
    register(state, "w1")

    provision(scheduler, "req-old", "sess-1")
    new = provision(scheduler, "req-new", "sess-1")
    scheduler.bind("req-new", "w1", "container-new")

    scheduler.release("req-old", Outcome.FAILURE)

    session = state.session("sess-1")
    assert session is not None
    assert session.affinity is not None
    assert session.affinity.seq == new.lease_id
