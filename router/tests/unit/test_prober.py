"""Worker liveness.

Registry traffic only flows during an execution, so an idle worker is silent
and a last-seen TTL alone would evict healthy nodes. Liveness is probed.
"""

from __future__ import annotations

import pytest

from readie_router.clock import FakeClock
from readie_router.scheduling.models import STATUS_READY, Affinity
from readie_router.scheduling.state import ClusterState
from readie_router.workers.prober import WorkerProber


class ScriptedHealth:
    """A health client with a per-target answer."""

    def __init__(self, answers: dict[str, bool]) -> None:
        self.answers = answers
        self.checked: list[str] = []

    async def check(self, target: str, *, timeout: float) -> bool:  # noqa: ASYNC109
        self.checked.append(target)
        return self.answers.get(target, True)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def state() -> ClusterState:
    return ClusterState()


def prober(state: ClusterState, clock: FakeClock, health: ScriptedHealth) -> WorkerProber:
    return WorkerProber(
        state=state,
        health=health,
        clock=clock,
        interval=5.0,
        timeout=1.0,
        failure_threshold=3,
    )


async def test_a_healthy_worker_survives(state: ClusterState, clock: FakeClock) -> None:
    state.apply_worker_status("w1", "w1:50052", STATUS_READY, clock.now())
    health = ScriptedHealth({"w1:50052": True})

    for _ in range(5):
        await prober(state, clock, health).probe_once()

    assert state.worker("w1") is not None


async def test_a_dead_worker_is_evicted_after_the_threshold(
    state: ClusterState, clock: FakeClock
) -> None:
    state.apply_worker_status("w1", "w1:50052", STATUS_READY, clock.now())
    health = ScriptedHealth({"w1:50052": False})
    probe = prober(state, clock, health)

    await probe.probe_once()
    await probe.probe_once()
    assert state.worker("w1") is not None, "two strikes must not be enough"

    await probe.probe_once()
    assert state.worker("w1") is None


async def test_a_recovering_worker_resets_its_strikes(
    state: ClusterState, clock: FakeClock
) -> None:
    state.apply_worker_status("w1", "w1:50052", STATUS_READY, clock.now())
    health = ScriptedHealth({"w1:50052": False})
    probe = prober(state, clock, health)

    await probe.probe_once()
    await probe.probe_once()

    health.answers["w1:50052"] = True
    await probe.probe_once()

    health.answers["w1:50052"] = False
    await probe.probe_once()
    await probe.probe_once()
    assert state.worker("w1") is not None, "the strike count must have reset"


async def test_every_worker_is_probed(state: ClusterState, clock: FakeClock) -> None:
    """Concurrently, so one hung worker cannot mask another's failure."""
    for i in range(3):
        state.apply_worker_status(f"w{i}", f"w{i}:50052", STATUS_READY, clock.now())
    health = ScriptedHealth({})

    await prober(state, clock, health).probe_once()

    assert sorted(health.checked) == ["w0:50052", "w1:50052", "w2:50052"]


async def test_evicting_a_worker_unpins_its_sessions(state: ClusterState, clock: FakeClock) -> None:
    state.apply_worker_status("w1", "w1:50052", STATUS_READY, clock.now())
    session = state.touch_session("sess-1", clock.now())
    session.affinity = Affinity(worker_id="w1", container_id="c1", seq=1)

    health = ScriptedHealth({"w1:50052": False})
    probe = prober(state, clock, health)
    for _ in range(3):
        await probe.probe_once()

    assert state.worker("w1") is None
    assert session.affinity is None


async def test_a_worker_that_readvertised_is_not_penalised(
    state: ClusterState, clock: FakeClock
) -> None:
    """The prober awaits between reading state and writing it back.

    If the worker re-registered on a new address in that window, the stale
    result must be discarded rather than counted against the new one.
    """
    state.apply_worker_status("w1", "old:50052", STATUS_READY, clock.now())

    failures = state.record_probe_result("w1", "old:50052", ok=False, now=clock.now())
    assert failures == 1

    state.apply_worker_status("w1", "new:50052", STATUS_READY, clock.now())
    stale = state.record_probe_result("w1", "old:50052", ok=False, now=clock.now())

    assert stale == 0, "a result for a stale address must not count"
