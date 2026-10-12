import asyncio

import pytest

from readie_playground.runner import EvalOptions, Lane, Runner
from tests.fakes import FakeBackend


def make(backend: FakeBackend, max_output: int = 1000) -> Runner:
    return Runner(backend, max_output)


async def test_both_lanes_run_every_step_with_the_right_start_flag() -> None:
    backend = FakeBackend()
    outcome = await make(backend).run_eval(["a = 1", "b = 2"], EvalOptions())

    assert set(outcome.lanes) == {Lane.OPTIMIZED, Lane.COLD}
    flags = {(c.index, c.cold) for c in backend.steps}
    assert flags == {(0, False), (1, False), (0, True), (1, True)}


async def test_steps_in_a_lane_share_one_session_and_the_lanes_never_share() -> None:
    backend = FakeBackend()
    outcome = await make(backend).run_eval(["a = 1", "b = 2", "c = 3"], EvalOptions())

    by_lane = {lane: [c for c in backend.steps if c.cold is (lane is Lane.COLD)] for lane in Lane}
    for lane, calls in by_lane.items():
        assert len({c.session_id for c in calls}) == 1
        assert [c.index for c in calls] == [0, 1, 2]  # one after another, in order
        assert calls[0].session_id == outcome.lanes[lane].session_id
    optimized, cold = (outcome.lanes[lane].session_id for lane in Lane)
    assert optimized != cold
    assert optimized is not None
    assert optimized.endswith("-optimized")
    assert cold is not None
    assert cold.endswith("-cold")
    assert outcome.session_id is not None
    assert optimized.startswith(outcome.session_id)


async def test_a_single_step_without_a_session_is_a_plain_call() -> None:
    backend = FakeBackend()
    outcome = await make(backend).run_eval(["a = 1"], EvalOptions())

    assert outcome.session_id is None
    assert {c.session_id for c in backend.steps} == {None}


async def test_a_caller_chosen_session_is_used_even_for_one_step() -> None:
    backend = FakeBackend()
    await make(backend).run_eval(["a = 1"], EvalOptions(), session="mine")
    assert {c.session_id for c in backend.steps} == {"mine-optimized", "mine-cold"}


async def test_the_two_lanes_run_in_parallel() -> None:
    # Each lane waits for the other inside its first step; run one after the other
    # and the barrier would never release.
    backend = FakeBackend(rendezvous=asyncio.Barrier(2))
    outcome = await make(backend).run_eval(["a = 1"], EvalOptions())
    assert all(result.ok for result in outcome.lanes.values())


async def test_every_option_reaches_the_backend_for_every_step() -> None:
    options = EvalOptions(
        memory="8Gi", max_memory="8Gi", packages=("lightgbm",), timeout_s=300.0, gpu=True
    )
    backend = FakeBackend()
    await make(backend).run_eval(["a = 1", "b = 2"], options)
    assert {c.options for c in backend.steps} == {options}


async def test_a_failing_step_is_recorded_and_the_next_step_still_runs() -> None:
    backend = FakeBackend(fail_steps=frozenset({1}))
    outcome = await make(backend).run_eval(["a = 1", "b = 2", "c = 3"], EvalOptions())

    steps = outcome.lanes[Lane.COLD].steps
    assert [s.ok for s in steps] == [True, False, True]
    assert steps[1].error_type == "RemoteExecutionError"
    assert "ValueError" in steps[1].error
    assert not outcome.lanes[Lane.COLD].ok


async def test_stopping_on_error_skips_the_rest_without_calling_readie() -> None:
    backend = FakeBackend(fail_steps=frozenset({0}))
    outcome = await make(backend).run_eval(
        ["a = 1", "b = 2", "c = 3"], EvalOptions(continue_on_error=False)
    )

    steps = outcome.lanes[Lane.OPTIMIZED].steps
    assert [s.error_type for s in steps] == ["RemoteExecutionError", "Skipped", "Skipped"]
    assert {c.index for c in backend.steps} == {0}
    assert steps[1].duration_s == 0.0


async def test_a_step_that_is_not_valid_python_is_reported_without_a_call() -> None:
    backend = FakeBackend()
    outcome = await make(backend).run_eval(["def broken(:", "b = 2"], EvalOptions())

    first = outcome.lanes[Lane.COLD].steps[0]
    assert (first.ok, first.error_type, first.duration_s) == (False, "SyntaxError", 0.0)
    assert "invalid syntax" in first.error
    assert {c.index for c in backend.steps} == {1}


async def test_error_kinds_are_told_apart() -> None:
    timeout = await make(FakeBackend(timeout_steps=frozenset({0}))).run_eval(["a"], EvalOptions())
    crash = await make(FakeBackend(crash_steps=frozenset({0}))).run_eval(["a"], EvalOptions())

    assert timeout.lanes[Lane.COLD].steps[0].error_type == "RemoteTimeoutError"
    assert crash.lanes[Lane.COLD].steps[0].error_type == "InternalError"
    assert "unexpected" in crash.lanes[Lane.COLD].steps[0].error


async def test_duration_is_the_clock_around_the_backend_call() -> None:
    class Clock:
        """Advances 2.5 s on every read, so each step reads (start, end) 2.5 s apart."""

        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            self.now += 2.5
            return self.now

    outcome = await Runner(FakeBackend(), 1000, clock=Clock()).run_eval(["a = 1"], EvalOptions())
    durations = sorted(result.steps[0].duration_s for result in outcome.lanes.values())
    # The fake backend never yields, so each lane reads the clock twice in a row.
    assert durations == [2.5, 2.5]


async def test_output_is_returned_and_cut_at_the_limit() -> None:
    outcome = await make(FakeBackend(output="x" * 50), max_output=20).run_eval(
        ["a = 1"], EvalOptions()
    )
    step = outcome.lanes[Lane.COLD].steps[0]
    assert step.output == "x" * 20
    assert step.truncated


async def test_total_is_the_sum_of_the_step_durations() -> None:
    outcome = await make(FakeBackend()).run_eval(["a = 1", "b = 2"], EvalOptions())
    lane = outcome.lanes[Lane.OPTIMIZED]
    assert lane.total_s == pytest.approx(sum(s.duration_s for s in lane.steps), abs=0.002)


async def test_cancelling_the_run_cancels_both_lanes() -> None:
    backend = FakeBackend(hang=True)
    task = asyncio.create_task(make(backend).run_eval(["a = 1"], EvalOptions()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.cancelled == 2


async def test_a_time_budget_for_the_lane_shortens_later_steps_and_stops_when_spent() -> None:
    class Clock:
        """Every backend call takes 4 s: the clock reads start and end 4 s apart."""

        def __init__(self) -> None:
            self.now = 0.0
            self.reads = 0

        def __call__(self) -> float:
            self.reads += 1
            if self.reads % 2 == 0:
                self.now += 4.0
            return self.now

    backend = FakeBackend()
    options = EvalOptions(timeout_s=30.0, total_timeout_s=10.0)
    outcome = await Runner(backend, 1000, clock=Clock()).run_eval(["a", "b", "c", "d"], options)

    cold_steps = outcome.lanes[Lane.COLD].steps
    assert [s.error_type for s in cold_steps][-1] == "OutOfTime"
    called = [c for c in backend.steps if c.cold]
    assert [round(c.options.timeout_s, 1) for c in called][:3] == [10.0, 6.0, 2.0]
