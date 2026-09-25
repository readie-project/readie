"""End-to-end CLI, offline: run a seeded corpus against the local target and report.

Uses the ``local`` target so no gVisor stack or API key is needed -- it drives the
real ``readie`` client's in-process path over generated (trusted) tasks.
"""

from __future__ import annotations

import pytest

from readie_evals.cli import main
from readie_evals.config import Settings
from readie_evals.corpus import CorpusStore
from readie_evals.models import Task


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("READIE_EVALS_OUT_DIR", str(tmp_path))
    for var in ("AZURE_ENDPOINT", "AZURE_API_KEY", "AZURE_MODEL_NAME"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings.from_env()
    CorpusStore(settings.corpus_path).append(
        [
            Task(
                source="generated",
                category="Machine Learning",
                code=("def task():\n    return 7\n",),
            ),
        ],
    )
    return tmp_path


def test_run_then_report_offline(state, capsys):
    assert main(["run", "--target", "local", "--run-id", "t"]) == 0
    assert main(["report", "--run-id", "t"]) == 0
    out = capsys.readouterr().out
    assert "Readie eval report" in out
    assert (state / "reports" / "report-t.md").exists()


def test_rerun_skips_completed_tasks(state):
    assert main(["run", "--target", "local", "--run-id", "t"]) == 0
    assert main(["run", "--target", "local", "--run-id", "t"]) == 0


def test_report_without_results_is_a_clean_miss(tmp_path, monkeypatch):
    monkeypatch.setenv("READIE_EVALS_OUT_DIR", str(tmp_path))
    assert main(["report"]) == 1


def test_run_without_corpus_is_a_clean_miss(tmp_path, monkeypatch):
    monkeypatch.setenv("READIE_EVALS_OUT_DIR", str(tmp_path))
    assert main(["run", "--target", "local"]) == 1


def test_run_exits_immediately_when_the_router_is_unreachable(state, capsys):
    # Nothing listens here, so the connection is refused immediately rather than
    # the run attempting -- and failing on -- every task one at a time.
    assert main(["run", "--router-uri", "127.0.0.1:1", "--run-id", "t"]) == 2
    assert "not reachable" in capsys.readouterr().out
