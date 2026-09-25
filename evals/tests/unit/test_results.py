from __future__ import annotations

import threading

from readie_evals.results import Result, ResultsStore


def _result(task_id="t1", run_id="r1", status="ok", **extra):
    base = {
        "task_id": task_id,
        "run_id": run_id,
        "source": "generated",
        "category": "ML",
        "status": status,
        "imports": ("numpy",),
        "packages": (),
    }
    return Result(**{**base, **extra})


def test_record_then_is_done(tmp_path):
    store = ResultsStore(tmp_path / "results.jsonl")
    assert not store.is_done("t1", "r1")
    store.record(_result(checkpoint_seconds=0.5))
    assert store.is_done("t1", "r1")


def test_is_done_survives_a_reopen(tmp_path):
    path = tmp_path / "results.jsonl"
    ResultsStore(path).record(_result())
    assert ResultsStore(path).is_done("t1", "r1")


def test_is_done_is_scoped_to_the_run(tmp_path):
    store = ResultsStore(tmp_path / "results.jsonl")
    store.record(_result(run_id="r1"))
    assert store.is_done("t1", "r1")
    assert not store.is_done("t1", "r2")


def test_should_skip_ignores_the_run_id(tmp_path):
    store = ResultsStore(tmp_path / "results.jsonl")
    assert not store.should_skip("t1")
    store.record(_result(task_id="t1", run_id="old-run", status="ok"))
    assert store.should_skip("t1"), (
        "recorded under a different run_id than a caller might ask about"
    )


def test_should_skip_survives_a_reopen(tmp_path):
    path = tmp_path / "results.jsonl"
    ResultsStore(path).record(_result(run_id="r1", status="ok"))
    assert ResultsStore(path).should_skip("t1")


def test_should_skip_is_false_for_a_failed_task(tmp_path):
    store = ResultsStore(tmp_path / "results.jsonl")
    store.record(_result(status="failed"))
    assert not store.should_skip("t1"), "a failure is not 'done' -- it stays eligible for retry"


def test_should_skip_turns_true_once_a_later_run_succeeds(tmp_path):
    store = ResultsStore(tmp_path / "results.jsonl")
    store.record(_result(run_id="r1", status="failed"))
    assert not store.should_skip("t1")
    store.record(_result(run_id="r2", status="repaired_ok"))
    assert store.should_skip("t1")


def test_rows_round_trip_the_fields(tmp_path):
    store = ResultsStore(tmp_path / "results.jsonl")
    store.record(_result(checkpoint_seconds=1.0, cold_start_seconds=3.0, payload_bytes=42))
    rows = store.rows("r1")
    assert len(rows) == 1
    assert rows[0]["checkpoint_seconds"] == 1.0
    assert rows[0]["cold_start_seconds"] == 3.0
    assert rows[0]["imports"] == ["numpy"]


def test_last_write_wins_on_the_key(tmp_path):
    store = ResultsStore(tmp_path / "results.jsonl")
    store.record(_result(status="failed"))
    store.record(_result(status="ok", checkpoint_seconds=0.2))
    rows = store.rows("r1")
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"


def test_run_ids_lists_present_runs(tmp_path):
    store = ResultsStore(tmp_path / "results.jsonl")
    store.record(_result(task_id="a", run_id="r1"))
    store.record(_result(task_id="b", run_id="r1"))
    store.record(_result(task_id="c", run_id="r2"))
    assert store.run_ids()[0] == "r1"


def test_concurrent_record_loses_no_rows(tmp_path):
    """No row is lost or corrupted when many threads call ``record`` at once.

    A concurrent run has many threads calling ``record`` at once; none of
    their writes -- to the file or to the in-memory bookkeeping -- may be
    lost or corrupted by another writer's interleaved write.
    """
    store = ResultsStore(tmp_path / "results.jsonl")
    n = 50
    barrier = threading.Barrier(n, timeout=5.0)

    def write(i: int) -> None:
        barrier.wait()  # maximise actual overlap between writers
        store.record(_result(task_id=f"t{i}", run_id="r1"))

    threads = [threading.Thread(target=write, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rows = store.rows("r1")
    assert len(rows) == n
    assert {row["task_id"] for row in rows} == {f"t{i}" for i in range(n)}
    reopened = ResultsStore(tmp_path / "results.jsonl")
    for i in range(n):
        assert reopened.is_done(f"t{i}", "r1")
