import asyncio

import pytest

from readie_playground.runner import EvalOptions, Runner
from tests.fakes import FakeBackend


def make(backend: FakeBackend, max_output: int = 1000) -> Runner:
    return Runner(backend, max_output)


async def test_every_step_runs_once_in_order() -> None:
    backend = FakeBackend()
    result = await make(backend).run_eval(["a = 1", "b = 2", "c = 3"], EvalOptions())

    assert [c.index for c in backend.steps] == [0, 1, 2]
    assert [c.source for c in backend.steps] == ["a = 1", "b = 2", "c = 3"]
    assert [s.index for s in result.steps] == [0, 1, 2]


async def test_the_remote_arguments_reach_every_step_untouched() -> None:
    # The runner decides nothing: no lanes, no start flag, no session of its own.
    remote = {
        "memory": "8Gi",
        "timeout": 300.0,
        "gpu": True,
        "packages": ["lightgbm==4.*", "numpy"],
        "disable_optimized_execution": True,
        "checkpoint_id": None,
    }
    options = EvalOptions(remote=remote)
    backend = FakeBackend()
    await make(backend).run_eval(["a = 1", "b = 2"], options)

    assert [c.options for c in backend.steps] == [options, options]
    assert all(c.options.remote == remote for c in backend.steps)


async def test_the_session_is_whatever_the_caller_sent_and_none_is_invented() -> None:
    backend = FakeBackend()
    none = await make(backend).run_eval(["a = 1", "b = 2"], EvalOptions())
    mine = await make(backend).run_eval(["a = 1"], EvalOptions(remote={"session": "mine"}))

    # Pinned: several steps once got a generated session; now they share one only
    # when the caller names it.
    assert none.session_id is None
    assert mine.session_id == "mine"


async def test_a_failing_step_is_recorded_and_the_next_step_still_runs() -> None:
    backend = FakeBackend(fail_steps=frozenset({1}))
    result = await make(backend).run_eval(["a = 1", "b = 2", "c = 3"], EvalOptions())

    assert [s.ok for s in result.steps] == [True, False, True]
    assert result.steps[1].error_type == "RemoteExecutionError"
    assert "ValueError" in result.steps[1].error
    assert not result.ok


async def test_stopping_on_error_skips_the_rest_without_calling_readie() -> None:
    backend = FakeBackend(fail_steps=frozenset({0}))
    result = await make(backend).run_eval(
        ["a = 1", "b = 2", "c = 3"], EvalOptions(continue_on_error=False)
    )

    assert [s.error_type for s in result.steps] == ["RemoteExecutionError", "Skipped", "Skipped"]
    assert {c.index for c in backend.steps} == {0}
    assert result.steps[1].duration_s == 0.0


async def test_a_step_that_is_not_valid_python_is_reported_without_a_call() -> None:
    backend = FakeBackend()
    result = await make(backend).run_eval(["def broken(:", "b = 2"], EvalOptions())

    first = result.steps[0]
    assert (first.ok, first.error_type, first.duration_s) == (False, "SyntaxError", 0.0)
    assert "invalid syntax" in first.error
    assert {c.index for c in backend.steps} == {1}


async def test_error_kinds_are_told_apart() -> None:
    timeout = await make(FakeBackend(timeout_steps=frozenset({0}))).run_eval(["a"], EvalOptions())
    crash = await make(FakeBackend(crash_steps=frozenset({0}))).run_eval(["a"], EvalOptions())

    assert timeout.steps[0].error_type == "RemoteTimeoutError"
    assert crash.steps[0].error_type == "InternalError"
    assert "unexpected" in crash.steps[0].error


async def test_duration_is_the_clock_around_the_backend_call() -> None:
    class Clock:
        """Advances 2.5 s on every read, so each step reads (start, end) 2.5 s apart."""

        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            self.now += 2.5
            return self.now

    result = await Runner(FakeBackend(), 1000, clock=Clock()).run_eval(["a = 1"], EvalOptions())

    assert result.steps[0].duration_s == 2.5


async def test_output_is_returned_and_cut_at_the_limit() -> None:
    result = await make(FakeBackend(output="x" * 50), max_output=20).run_eval(
        ["a = 1"], EvalOptions()
    )
    step = result.steps[0]
    assert step.output == "x" * 20
    assert step.truncated


async def test_total_is_the_sum_of_the_step_durations() -> None:
    result = await make(FakeBackend()).run_eval(["a = 1", "b = 2"], EvalOptions())
    assert result.total_s == pytest.approx(sum(s.duration_s for s in result.steps), abs=0.002)


async def test_cancelling_the_run_cancels_the_step_in_flight() -> None:
    backend = FakeBackend(hang=True)
    task = asyncio.create_task(make(backend).run_eval(["a = 1"], EvalOptions()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.cancelled == 1
