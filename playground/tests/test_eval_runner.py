import asyncio

import pytest

from readie_playground.runner import EvalOptions, Runner
from tests.fakes import FakeBackend


def make(backend: FakeBackend, max_output: int = 1000) -> Runner:
    return Runner(backend, max_output)


async def test_the_code_runs_once() -> None:
    backend = FakeBackend()
    result = await make(backend).run_eval("a = 1", EvalOptions())

    assert [c.source for c in backend.evals] == ["a = 1"]
    assert result.ok
    assert (result.error, result.error_type) == ("", "")


async def test_the_remote_arguments_reach_the_backend_untouched() -> None:
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
    await make(backend).run_eval("a = 1", options)

    assert [c.options for c in backend.evals] == [options]
    assert backend.evals[0].options.remote == remote


async def test_the_session_is_whatever_the_caller_sent_and_none_is_invented() -> None:
    backend = FakeBackend()
    none = await make(backend).run_eval("a = 1", EvalOptions())
    mine = await make(backend).run_eval("a = 1", EvalOptions(remote={"session": "mine"}))

    assert none.session_id is None
    assert mine.session_id == "mine"


async def test_a_failure_is_recorded_not_raised() -> None:
    result = await make(FakeBackend(fail_eval=True)).run_eval("a = 1", EvalOptions())

    assert not result.ok
    assert result.error_type == "RemoteExecutionError"
    assert "ValueError" in result.error


async def test_code_that_is_not_valid_python_is_reported_without_a_call() -> None:
    backend = FakeBackend()
    result = await make(backend).run_eval("def broken(:", EvalOptions())

    assert (result.ok, result.error_type, result.duration_s) == (False, "SyntaxError", 0.0)
    assert "invalid syntax" in result.error
    assert backend.evals == []


async def test_error_kinds_are_told_apart() -> None:
    timeout = await make(FakeBackend(timeout_eval=True)).run_eval("a", EvalOptions())
    crash = await make(FakeBackend(crash_eval=True)).run_eval("a", EvalOptions())

    assert timeout.error_type == "RemoteTimeoutError"
    assert crash.error_type == "InternalError"
    assert "unexpected" in crash.error


async def test_duration_is_the_clock_around_the_backend_call() -> None:
    class Clock:
        """Advances 2.5 s on every read, so the call reads (start, end) 2.5 s apart."""

        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            self.now += 2.5
            return self.now

    result = await Runner(FakeBackend(), 1000, clock=Clock()).run_eval("a = 1", EvalOptions())

    assert result.duration_s == 2.5


async def test_output_is_returned_and_cut_at_the_limit() -> None:
    result = await make(FakeBackend(output="x" * 50), max_output=20).run_eval(
        "a = 1", EvalOptions()
    )

    assert result.output == "x" * 20
    assert result.truncated


async def test_cancelling_the_run_cancels_the_call_in_flight() -> None:
    backend = FakeBackend(hang=True)
    task = asyncio.create_task(make(backend).run_eval("a = 1", EvalOptions()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.cancelled == 1


async def test_requests_in_one_session_share_a_namespace() -> None:
    # Pinned: every request once got a fresh uuid key, so a name defined by step 1 was a
    # NameError in step 2 even in the same session, against what the README promises.
    backend = FakeBackend()
    runner = make(backend)
    await runner.run_eval("a = 1", EvalOptions(remote={"session": "mine"}))
    await runner.run_eval("print(a)", EvalOptions(remote={"session": "mine"}))

    assert [c.key for c in backend.evals] == ["mine", "mine"]


async def test_different_sessions_do_not_share_a_namespace() -> None:
    backend = FakeBackend()
    runner = make(backend)
    await runner.run_eval("a = 1", EvalOptions(remote={"session": "one"}))
    await runner.run_eval("a = 1", EvalOptions(remote={"session": "two"}))

    assert [c.key for c in backend.evals] == ["one", "two"]


async def test_requests_with_no_session_are_independent() -> None:
    backend = FakeBackend()
    runner = make(backend)
    await runner.run_eval("a = 1", EvalOptions())
    await runner.run_eval("a = 1", EvalOptions(remote={"session": None}))

    keys = [c.key for c in backend.evals]
    assert len(set(keys)) == 2
    assert all(key and key != "None" for key in keys)
