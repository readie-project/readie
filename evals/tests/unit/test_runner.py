from __future__ import annotations

import threading

from readie_evals.config import Settings
from readie_evals.execution import ExecOutcome, Executor, Target
from readie_evals.models import Task
from readie_evals.results import ResultsStore
from readie_evals.runner import Runner

_GOOD = ("def task():\n    return 1\n",)
_BAD = ("def task():\n    raise ValueError('boom')\n",)


def _runner(tmp_path, repair=None):
    settings = Settings.from_env(env={})
    results = ResultsStore(tmp_path / "results.db")
    runner = Runner(
        Executor(settings, target=Target.LOCAL),
        results,
        target=Target.LOCAL,
        repair=repair,
        on_line=lambda _line: None,
    )
    return runner, results


def test_a_good_task_is_recorded_ok(tmp_path):
    runner, results = _runner(tmp_path)
    task = Task(source="generated", category="ML", code=_GOOD)
    stats = runner.run([task], run_id="r1")
    assert stats.ok == 1
    assert next(iter(results.rows("r1")))["status"] == "ok"
    results.close()


def test_a_failure_without_a_repairer_is_recorded_failed(tmp_path):
    runner, results = _runner(tmp_path)
    stats = runner.run([Task(source="generated", category="ML", code=_BAD)], run_id="r1")
    assert stats.failed == 1
    row = next(iter(results.rows("r1")))
    assert row["status"] == "failed"
    assert row["error_type"] == "ValueError"
    results.close()


def test_a_repaired_failure_is_recorded_repaired(tmp_path):
    def repair(category, code, error_type, error_message):
        return _GOOD[0], ()

    runner, results = _runner(tmp_path, repair=repair)
    stats = runner.run([Task(source="generated", category="ML", code=_BAD)], run_id="r1")
    assert stats.repaired == 1
    row = next(iter(results.rows("r1")))
    assert row["status"] == "repaired_ok"
    assert row["repaired"] is True
    results.close()


def test_a_repair_that_also_fails_is_left_failed(tmp_path):
    def repair(category, code, error_type, error_message):
        return _BAD[0], ()  # "fixed" code that still raises

    runner, results = _runner(tmp_path, repair=repair)
    stats = runner.run([Task(source="generated", category="ML", code=_BAD)], run_id="r1")
    assert stats.failed == 1
    results.close()


def test_a_completed_task_is_skipped_on_rerun(tmp_path):
    runner, results = _runner(tmp_path)
    task = Task(source="generated", category="ML", code=_GOOD)
    runner.run([task], run_id="r1")
    stats = runner.run([task], run_id="r1")
    assert stats.skipped == 1
    assert stats.ok == 0
    results.close()


def test_a_completed_task_is_skipped_under_a_different_run_id(tmp_path):
    """A task measured under an earlier run_id must not re-run under a new one.

    Growing the corpus (``sample``) shifts the default run_id, so this is the case
    that actually matters day to day.
    """
    runner, results = _runner(tmp_path)
    task = Task(source="generated", category="ML", code=_GOOD)
    runner.run([task], run_id="r1")
    stats = runner.run([task], run_id="r2")
    assert stats.skipped == 1
    assert stats.ok == 0
    results.close()


def test_a_failed_task_is_retried_on_rerun_not_skipped(tmp_path):
    runner, results = _runner(tmp_path)
    task = Task(source="generated", category="ML", code=_BAD)
    runner.run([task], run_id="r1")
    stats = runner.run([task], run_id="r2")
    assert stats.skipped == 0
    assert stats.failed == 1
    results.close()


def test_local_target_skips_untrusted_sources(tmp_path):
    runner, results = _runner(tmp_path)
    task = Task(source="kaggle", category="ML", code=_GOOD)
    stats = runner.run([task], run_id="r1")
    assert stats.skipped == 1
    assert stats.attempted == 0
    results.close()


def test_local_target_trusts_generated_session_sources(tmp_path):
    runner, results = _runner(tmp_path)
    task = Task(
        source="generated-notebook",
        category="ML",
        code=_GOOD,
        session_kind="notebook",
    )
    stats = runner.run([task], run_id="r1")
    assert stats.ok == 1
    results.close()


class _BarrierExecutor:
    """A fake ``Executor`` whose ``run`` blocks until ``n`` calls arrive at once.

    Proves tasks actually overlap: if the runner dispatched them one at a
    time instead of concurrently, the barrier would never fill and time out.
    """

    def __init__(self, n: int) -> None:
        self._barrier = threading.Barrier(n, timeout=1.0)

    def run(self, *, code, memory, packages, timeout, repair=None):
        self._barrier.wait()
        return ExecOutcome(
            checkpoint_seconds=0.0, cold_start_seconds=None, value_repr="1", payload_bytes=None
        )


def test_concurrency_runs_multiple_tasks_at_once(tmp_path):
    results = ResultsStore(tmp_path / "results.db")
    runner = Runner(
        _BarrierExecutor(3),  # type: ignore[arg-type]
        results,
        target=Target.LOCAL,
        on_line=lambda _line: None,
        concurrency=3,
    )
    tasks = [
        Task(source="generated", category="ML", code=(f"def task():\n    return {i}\n",))
        for i in range(3)
    ]
    stats = runner.run(tasks, run_id="r1")
    assert stats.ok == 3
    assert len(results.rows("r1")) == 3
    results.close()


def test_concurrency_aggregates_stats_correctly_across_many_tasks(tmp_path):
    """Every dispatched task's outcome is tallied exactly once.

    No update is lost to concurrent ``stats`` mutation across many threads.
    """
    results = ResultsStore(tmp_path / "results.db")
    settings = Settings.from_env(env={})
    runner = Runner(
        Executor(settings, target=Target.LOCAL),
        results,
        target=Target.LOCAL,
        on_line=lambda _line: None,
        concurrency=8,
    )
    good = [
        Task(source="generated", category="ML", code=(f"def task():\n    return {i}\n",))
        for i in range(20)
    ]
    bad = [
        Task(
            source="generated",
            category="ML",
            code=(f"def task():\n    raise ValueError('boom {i}')\n",),
        )
        for i in range(15)
    ]
    stats = runner.run(good + bad, run_id="r1")
    assert stats.ok == 20
    assert stats.failed == 15
    assert stats.attempted == 35
    assert len(results.rows("r1")) == 35
    results.close()


def test_concurrency_respects_limit(tmp_path):
    results = ResultsStore(tmp_path / "results.db")
    settings = Settings.from_env(env={})
    runner = Runner(
        Executor(settings, target=Target.LOCAL),
        results,
        target=Target.LOCAL,
        on_line=lambda _line: None,
        concurrency=4,
    )
    tasks = [
        Task(source="generated", category="ML", code=(f"def task():\n    return {i}\n",))
        for i in range(10)
    ]
    stats = runner.run(tasks, run_id="r1", limit=4)
    assert stats.attempted == 4
    results.close()


def test_a_session_tasks_failing_cell_is_repaired_in_place_and_the_sequence_continues(tmp_path):
    calls = []

    def repair(category, code, error_type, error_message):
        calls.append((category, code, error_type, error_message))
        return "def task():\n    return 2\n", ()

    runner, results = _runner(tmp_path, repair=repair)
    task = Task(
        source="generated-retry",
        category="ML",
        code=("def task():\n    return 1\n", *_BAD, "def task():\n    return 3\n"),
        session_kind="retry",
    )
    stats = runner.run([task], run_id="r1")

    assert stats.repaired == 1
    assert len(calls) == 1
    assert calls[0][0] == "ML"
    row = next(iter(results.rows("r1")))
    assert row["status"] == "repaired_ok"
    assert row["session_kind"] == "retry"
    assert row["cells"] == 3
    results.close()
