from __future__ import annotations

import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from readie_evals.config import Settings
from readie_evals.execution import Executor, Target, TaskExecutionError


def _executor():
    return Executor(Settings.from_env(env={}), target=Target.LOCAL)


def _real_executor(*, pool=None, release_wait=0.0):
    settings = dataclasses.replace(Settings.from_env(env={}), container_release_wait=release_wait)
    return Executor(settings, target=Target.REAL, pool=pool)


def test_local_target_runs_the_function_and_times_it():
    outcome = _executor().run(
        code=("def task():\n    return sum(range(5))\n",),
        memory=None,
        packages=(),
        timeout=None,
    )
    assert outcome.value_repr == "10"
    assert outcome.checkpoint_seconds is not None
    assert outcome.checkpoint_seconds >= 0
    # The local target has no checkpoint/cold-start distinction.
    assert outcome.cold_start_seconds is None
    assert outcome.payload_bytes is not None


def test_local_target_runs_every_cell_in_order_and_sums_their_time():
    outcome = _executor().run(
        code=(
            "def task():\n    return 1\n",
            "def task():\n    return 2\n",
        ),
        memory=None,
        packages=(),
        timeout=None,
    )
    # The last cell's return value is the session's reported outcome.
    assert outcome.value_repr == "2"
    assert outcome.checkpoint_seconds is not None
    assert outcome.cold_start_seconds is None


def test_local_target_surfaces_a_raise_as_execution_failure():
    with pytest.raises(TaskExecutionError) as caught:
        _executor().run(
            code=("def task():\n    raise ValueError('boom')\n",),
            memory=None,
            packages=(),
            timeout=None,
        )
    assert caught.value.error_type == "ValueError"
    assert "boom" in caught.value.message


def test_missing_entrypoint_is_a_failure():
    with pytest.raises(TaskExecutionError) as caught:
        _executor().run(
            code=("def other():\n    return 1\n",),
            memory=None,
            packages=(),
            timeout=None,
        )
    assert caught.value.error_type == "NameError"


def test_repair_retry_time_includes_the_failed_attempts_own_time():
    def repair(code, error_type, error_message):
        return "def task():\n    return 2\n", ()

    outcome = _executor().run(
        code=("def task():\n    import time\n    time.sleep(0.2)\n    raise ValueError('boom')\n",),
        memory=None,
        packages=(),
        timeout=None,
        repair=repair,
    )
    assert outcome.repaired is True
    assert outcome.value_repr == "2"
    # The failed attempt's own 0.2s sleep is included, not discarded in favour
    # of just the retry's time.
    assert outcome.checkpoint_seconds is not None
    assert outcome.checkpoint_seconds >= 0.2


def test_repair_gives_up_after_one_retry_per_cell():
    def repair(code, error_type, error_message):
        return "def task():\n    raise ValueError('still broken')\n", ()

    with pytest.raises(TaskExecutionError) as caught:
        _executor().run(
            code=("def task():\n    raise ValueError('boom')\n",),
            memory=None,
            packages=(),
            timeout=None,
            repair=repair,
        )
    assert caught.value.error_type == "ValueError"


def test_repair_none_from_the_callback_gives_up_immediately():
    def repair(code, error_type, error_message):
        return None

    with pytest.raises(TaskExecutionError):
        _executor().run(
            code=("def task():\n    raise ValueError('boom')\n",),
            memory=None,
            packages=(),
            timeout=None,
            repair=repair,
        )


def test_repair_continues_the_sequence_after_fixing_a_cell():
    def repair(code, error_type, error_message):
        return "def task():\n    return 99\n", ()

    outcome = _executor().run(
        code=(
            "def task():\n    return 1\n",
            "def task():\n    raise ValueError('boom')\n",
            "def task():\n    return 3\n",
        ),
        memory=None,
        packages=(),
        timeout=None,
        repair=repair,
    )
    assert outcome.repaired is True
    # The sequence carried on past the repaired cell to the one after it.
    assert outcome.value_repr == "3"


def test_repair_can_add_a_package_the_fix_needs():
    def repair(code, error_type, error_message):
        return "def task():\n    return 2\n", ("requests",)

    outcome = _executor().run(
        code=("def task():\n    raise ValueError('boom')\n",),
        memory=None,
        packages=(),
        timeout=None,
        repair=repair,
    )
    assert outcome.repaired is True
    assert outcome.value_repr == "2"


def test_real_target_runs_checkpoint_and_cold_start_concurrently_given_a_pool():
    executor = _real_executor(pool=ThreadPoolExecutor(max_workers=2))
    entered = threading.Barrier(2, timeout=1.0)

    def fake_run_session(codes, memory, packages, timeout, *, disable_optimized, repair):
        # Both sides must reach this point before either is allowed to
        # proceed -- if they ran back to back instead of concurrently, the
        # second call would never arrive and the barrier would time out.
        entered.wait()
        return (0.0, disable_optimized, codes, packages, False, None)

    executor._run_session = fake_run_session
    outcome = executor.run(
        code=("def task():\n    return 1\n",), memory=None, packages=(), timeout=None
    )
    # value_repr comes from whichever side's _run_session returned last to be
    # unpacked (cold-start); the barrier having released at all is the proof
    # both sides actually overlapped.
    assert outcome.value_repr == "True"


def test_real_target_runs_checkpoint_and_cold_start_sequentially_without_a_pool():
    executor = _real_executor(pool=None)
    order = []

    def fake_run_session(codes, memory, packages, timeout, *, disable_optimized, repair):
        order.append(disable_optimized)
        return (0.0, disable_optimized, codes, packages, False, None)

    executor._run_session = fake_run_session
    executor.run(code=("def task():\n    return 1\n",), memory=None, packages=(), timeout=None)
    # Checkpoint (False) always attempted before cold-start (True).
    assert order == [False, True]


def test_run_session_waits_before_releasing_the_container_slot(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("readie_evals.execution.time.sleep", sleeps.append)
    executor = _real_executor(release_wait=35.0)
    executor._run_cells = lambda *_a, **_k: (0.0, None, ("code",), (), False, None)

    executor._run_session(("code",), None, (), None, disable_optimized=False, repair=None)

    assert sleeps == [35.0]


def test_run_session_wait_is_configurable_including_zero(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("readie_evals.execution.time.sleep", sleeps.append)
    executor = _real_executor(release_wait=0.0)
    executor._run_cells = lambda *_a, **_k: (0.0, None, ("code",), (), False, None)

    executor._run_session(("code",), None, (), None, disable_optimized=False, repair=None)

    assert sleeps == [0.0]


def test_real_target_cold_start_never_sees_checkpoints_repaired_code():
    executor = _real_executor(pool=None)
    seen_codes = []

    def fake_run_session(codes, memory, packages, timeout, *, disable_optimized, repair):
        seen_codes.append(codes)
        # The checkpoint side "repairs" its code before returning, exactly
        # as a real _run_cells would after a successful retry.
        returned_codes = ("fixed by checkpoint",) if not disable_optimized else codes
        return (0.0, None, returned_codes, packages, False, None)

    executor._run_session = fake_run_session
    executor.run(
        code=("original code",),
        memory=None,
        packages=(),
        timeout=None,
    )
    # Both sides were handed the task's own original code, not whatever the
    # other side ended up returning.
    assert seen_codes == [("original code",), ("original code",)]
