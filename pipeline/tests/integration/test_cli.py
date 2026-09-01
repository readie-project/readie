"""The command line, end to end where it does not need runsc."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from readie_pipeline.cli import main
from readie_pipeline.corpus.models import Corpus, Request

DATA = Path(__file__).parents[2] / "data"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "out"


def run(*argv: str) -> int:
    return main(list(argv))


def test_plan_produces_a_plan_from_the_committed_corpus(workspace: Path, capsys):
    # No runsc, no network, no container: this is the stage to iterate on, and
    # neither of the previous module-scope scripts could be run alone at all.
    code = run(
        "--data-dir",
        str(DATA),
        "--output-dir",
        str(workspace),
        "plan",
        "--no-spec",
        "--max-checkpoints",
        "2",
        "--size-budget-mb",
        "300",
    )

    assert code == 0
    plan = json.loads((workspace / "checkpoints.json").read_text())
    assert 1 <= len(plan) <= 2
    assert plan[0]["imports"]
    assert plan[0]["requests_served"] > 0
    assert "planning over" in capsys.readouterr().out


def test_plan_is_reproducible(workspace: Path):
    args = (
        "--data-dir",
        str(DATA),
        "--output-dir",
        str(workspace),
        "plan",
        "--no-spec",
        "--max-checkpoints",
        "3",
        "--size-budget-mb",
        "300",
    )
    run(*args)
    first = (workspace / "checkpoints.json").read_text()
    run(*args)

    assert (workspace / "checkpoints.json").read_text() == first


def test_the_fixed_planner_is_selectable(workspace: Path):
    run(
        "--data-dir",
        str(DATA),
        "--output-dir",
        str(workspace),
        "plan",
        "--no-spec",
        "--planner",
        "fixed",
    )
    plan = json.loads((workspace / "checkpoints.json").read_text())

    assert plan[0]["imports"] == ["pandas", "numpy"]


def test_an_unknown_planner_is_reported_not_raised(workspace: Path, capsys):
    # argparse rejects it first, which is the better error, but the settings
    # path has to be safe too.
    with pytest.raises(SystemExit):
        run("--output-dir", str(workspace), "plan", "--planner", "telepathy")


def test_a_missing_corpus_explains_itself(tmp_path: Path, capsys):
    code = run("--data-dir", str(tmp_path), "--output-dir", str(tmp_path), "plan", "--no-spec")

    assert code == 1
    assert "readie-pipeline corpus" in capsys.readouterr().err


def test_build_without_a_plan_says_to_plan_first(workspace: Path, capsys):
    code = run("--data-dir", str(DATA), "--output-dir", str(workspace), "build")

    assert code == 1
    assert "run `readie-pipeline plan` first" in capsys.readouterr().err


def test_capture_plans_before_it_captures(workspace: Path, tmp_path: Path, capsys, monkeypatch):
    # `capture` exists because the image's entrypoint cannot plan at build time:
    # the capture mounts a host directory over the output path and a bind mount
    # hides whatever the image wrote there. So it must plan itself, and the proof
    # is that it gets past planning to a stage that needs a binary this machine
    # has no reason to have.
    monkeypatch.setenv("OCISPEC_BINARY", str(tmp_path / "no-such-ocispec"))
    bundle = tmp_path / "executorfs"
    bundle.mkdir()

    code = run(
        "--data-dir",
        str(DATA),
        "--output-dir",
        str(workspace),
        "--bundle-dir",
        str(bundle),
        "capture",
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "no-such-ocispec" in err
    assert "run `readie-pipeline plan` first" not in err, "capture must plan, not demand a plan"
    assert (workspace / "checkpoints.json").is_file()


def test_the_plan_can_be_kept_out_of_the_output_directory(workspace: Path, tmp_path: Path):
    # The image does this: output_dir is mounted out and copied into the worker
    # image wholesale, and the plan is an input to a capture rather than
    # something the worker should ship beside its manifest.
    plans = tmp_path / "plan"

    code = run(
        "--data-dir",
        str(DATA),
        "--output-dir",
        str(workspace),
        "--plan-dir",
        str(plans),
        "plan",
        "--no-spec",
    )

    assert code == 0
    assert (plans / "checkpoints.json").is_file()
    assert not (workspace / "checkpoints.json").exists()


def test_a_traceback_is_never_printed_for_an_expected_failure(tmp_path: Path, capsys):
    # Every expected failure already explains itself; a traceback would bury the
    # explanation in argparse frames.
    run("--data-dir", str(tmp_path), "--output-dir", str(tmp_path), "plan", "--no-spec")

    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert captured.err.startswith("error: ")


def test_plan_works_on_a_synthetic_corpus(tmp_path: Path):
    # Proves the stage depends on nothing but its inputs.
    data = tmp_path / "data"
    data.mkdir()
    corpus = Corpus.of(
        [Request(task_name=f"t{i}", category="c", code="", imports=("pandas",)) for i in range(50)]
    )
    (data / "dataset.json").write_text(json.dumps([r.to_json() for r in corpus]))
    (data / "metadata.json").write_text(
        json.dumps({"pandas": {"base_import": "pandas", "disk_size_mb": 40, "import_time": 0.3}})
    )

    code = run("--data-dir", str(data), "--output-dir", str(tmp_path / "out"), "plan", "--no-spec")

    assert code == 0
    plan = json.loads((tmp_path / "out" / "checkpoints.json").read_text())
    assert plan[0]["imports"] == ["pandas"]
    assert plan[0]["requests_served"] == 50


def test_the_help_lists_every_stage(capsys):
    with pytest.raises(SystemExit):
        run("--help")

    out = capsys.readouterr().out
    for stage in ("corpus", "analyze", "plan", "build", "capture"):
        assert stage in out
