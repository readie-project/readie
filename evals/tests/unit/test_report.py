from __future__ import annotations

from readie_evals.report import build_markdown, write_reports
from readie_evals.results import Result, ResultsStore


def _rows():
    return [
        {
            "task_id": "a",
            "run_id": "r1",
            "source": "generated",
            "category": "ML",
            "status": "ok",
            "checkpoint_seconds": 1.0,
            "cold_start_seconds": 4.0,
            "imports": ["numpy", "sklearn"],
            "packages": [],
            "error_type": None,
            "error_message": None,
            "repaired": False,
            "payload_bytes": 100,
            "value_repr": "1",
            "created_at": "now",
        },
        {
            "task_id": "b",
            "run_id": "r1",
            "source": "kaggle",
            "category": "ML",
            "status": "failed",
            "checkpoint_seconds": None,
            "cold_start_seconds": None,
            "imports": ["numpy"],
            "packages": [],
            "error_type": "ValueError",
            "error_message": "boom",
            "repaired": False,
            "payload_bytes": None,
            "value_repr": None,
            "created_at": "now",
        },
    ]


def test_markdown_summarizes_counts_and_dimensions():
    md = build_markdown(_rows(), "r1")
    assert "run `r1`" in md
    assert "Tasks: **2**" in md
    assert "### By category" in md
    assert "### By source" in md
    assert "numpy" in md  # the import table


def test_markdown_reports_the_checkpoint_speedup():
    md = build_markdown(_rows(), "r1")
    # cold-start 4.0s over checkpoint 1.0s is a 4x speedup.
    assert "4.0x" in md


def test_markdown_handles_no_rows():
    md = build_markdown([], "empty")
    assert "Tasks: **0**" in md


def test_write_reports_creates_both_files(tmp_path):
    store = ResultsStore(tmp_path / "results.jsonl")
    store.record(
        Result(
            task_id="a",
            run_id="r1",
            source="generated",
            category="ML",
            status="ok",
            imports=("numpy",),
            packages=(),
            checkpoint_seconds=0.5,
            cold_start_seconds=2.0,
        ),
    )
    csv_path, md_path = write_reports(store, "r1", tmp_path / "reports")
    assert csv_path.exists()
    assert md_path.exists()
    assert "run `r1`" in md_path.read_text()
