"""The command line, end to end where it does not need runsc."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from readie_pipeline.cli import main
from tests.helpers import item, write_data_dir


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return write_data_dir(tmp_path / "data")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "out"


def run(*argv: str) -> int:
    return main(list(argv))


def test_plan_produces_a_plan_from_a_workload_corpus(data_dir: Path, workspace: Path, capsys):
    # No runsc, no network, no container: this is the stage to iterate on, and
    # neither of the previous module-scope scripts could be run alone at all.
    code = run(
        "--data-dir",
        str(data_dir),
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
    # A single facility's own top-level ask can be filtered out as ineligible
    # even while its shared dependencies remain (see MIN_COVERAGE), so it's
    # the plan as a whole -- not necessarily its first entry -- that must
    # report something the executor can act on.
    assert any(p["imports"] for p in plan)
    assert plan[0]["requests_served"] > 0
    assert "planning over" in capsys.readouterr().out


def test_plan_is_reproducible(data_dir: Path, workspace: Path):
    args = (
        "--data-dir",
        str(data_dir),
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


def test_the_fixed_planner_is_selectable(data_dir: Path, workspace: Path):
    run(
        "--data-dir",
        str(data_dir),
        "--output-dir",
        str(workspace),
        "plan",
        "--no-spec",
        "--planner",
        "fixed",
    )
    plan = json.loads((workspace / "checkpoints.json").read_text())

    assert plan[0]["imports"] == ["pandas", "numpy"]


def test_an_unknown_planner_is_reported_not_raised(data_dir: Path, workspace: Path, capsys):
    # argparse rejects it first, which is the better error, but the settings
    # path has to be safe too.
    with pytest.raises(SystemExit):
        run("--output-dir", str(workspace), "plan", "--planner", "telepathy")


def test_a_missing_corpus_explains_itself(tmp_path: Path, capsys):
    code = run("--data-dir", str(tmp_path), "--output-dir", str(tmp_path), "plan", "--no-spec")

    assert code == 1
    assert "readie-pipeline corpus" in capsys.readouterr().err


def test_build_without_a_plan_says_to_plan_first(data_dir: Path, workspace: Path, capsys):
    code = run("--data-dir", str(data_dir), "--output-dir", str(workspace), "build")

    assert code == 1
    assert "run `readie-pipeline plan` first" in capsys.readouterr().err


def test_capture_plans_before_it_captures(
    data_dir: Path, workspace: Path, tmp_path: Path, capsys, monkeypatch
):
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
        str(data_dir),
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


def test_the_plan_can_be_kept_out_of_the_output_directory(
    data_dir: Path, workspace: Path, tmp_path: Path
):
    # The image does this: output_dir is mounted out and copied into the worker
    # image wholesale, and the plan is an input to a capture rather than
    # something the worker should ship beside its manifest.
    plans = tmp_path / "plan"

    code = run(
        "--data-dir",
        str(data_dir),
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
    (data / "datasets").mkdir(parents=True)
    (data / "metadata").mkdir()
    corpus = [item("pandas") for _ in range(50)]
    (data / "datasets" / "cpu.json").write_text(json.dumps([i.to_json() for i in corpus]))
    (data / "metadata" / "cpu.json").write_text(
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


def test_corpus_dry_run_prints_the_planned_mix_without_credentials(
    tmp_path: Path, capsys, monkeypatch
):
    for name in ("AZURE_ENDPOINT", "AZURE_API_KEY", "AZURE_MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)

    code = run("--data-dir", str(tmp_path), "corpus", "--dry-run", "--total", "70")

    out = capsys.readouterr().out
    assert code == 0
    assert "cpu corpus, adding 70 items" in out
    for source in ("kaggle", "kaggle_notebook", "huggingface", "agent_fix"):
        assert f"{source}:" in out
    assert not (tmp_path / "datasets").exists()


def test_corpus_dry_run_can_be_limited_to_one_source(tmp_path: Path, capsys):
    run(
        "--data-dir", str(tmp_path), "corpus", "--dry-run", "--total", "10", "--source", "agent_fix"
    )
    out = capsys.readouterr().out
    assert "agent_fix: 10" in out
    assert "kaggle: 0" in out


def test_corpus_without_model_credentials_names_them(tmp_path: Path, capsys, monkeypatch):
    for name in ("AZURE_ENDPOINT", "AZURE_API_KEY", "AZURE_MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)
    run("--data-dir", str(tmp_path), "corpus")
    assert "AZURE_ENDPOINT" in capsys.readouterr().err


def test_the_base_images_measured_packages_are_handed_to_the_corpus_stage(tmp_path: Path):
    from readie_pipeline.cli import _installed_packages
    from readie_pipeline.config import Settings

    data = write_data_dir(tmp_path / "data")
    known = _installed_packages(Settings.from_env({"READIE_DATA_DIR": str(data)}))
    assert "pandas" in known
    assert "lambdaTTS" not in known


def test_a_flavor_that_was_never_analyzed_has_no_known_packages(tmp_path: Path):
    from readie_pipeline.cli import _installed_packages
    from readie_pipeline.config import Settings

    settings = Settings.from_env({"READIE_DATA_DIR": str(tmp_path), "FLAVOR": "gpu"})
    assert _installed_packages(settings) == frozenset()


def test_clean_without_a_model_fixes_notebook_syntax_in_place(tmp_path: Path, capsys, monkeypatch):
    for name in ("AZURE_ENDPOINT", "AZURE_API_KEY", "AZURE_MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)
    corpus_file = tmp_path / "datasets" / "cpu.json"
    corpus_file.parent.mkdir(parents=True)
    bad = item("pandas")
    bad = type(bad)(
        ("%matplotlib inline\nimport pandas as pd",), bad.domain, bad.source, bad.resources
    )
    corpus_file.write_text(json.dumps([bad.to_json()]))

    code = run("--data-dir", str(tmp_path), "corpus", "--clean", "--no-rewrite")

    out = capsys.readouterr().out
    assert code == 0
    assert "notebook syntax or paths fixed in 1 items" in out
    assert "magic:matplotlib: 1" in out
    stored = json.loads(corpus_file.read_text())[0]["code"][0]
    assert stored.startswith("# [readie] removed: %matplotlib inline")


def test_clean_with_the_rewrite_on_needs_the_model_credentials(tmp_path: Path, capsys, monkeypatch):
    for name in ("AZURE_ENDPOINT", "AZURE_API_KEY", "AZURE_MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "cpu.json").write_text("[]")

    run("--data-dir", str(tmp_path), "corpus", "--clean")

    assert "AZURE_ENDPOINT" in capsys.readouterr().err


def test_the_corpus_stage_can_write_to_another_file(tmp_path: Path, capsys):
    target = tmp_path / "evals" / "cpu_eval.json"
    run("--data-dir", str(tmp_path), "corpus", "--dry-run", "--total", "7", "--output", str(target))
    assert f"-> {target}" in capsys.readouterr().out


def test_eval_report_summarises_a_results_file(tmp_path: Path, capsys):
    results = tmp_path / "r.jsonl"
    steps = [{"index": 0, "ok": True, "duration_s": 2.0, "error": "", "error_type": ""}]
    lane = {"ok": True, "total_s": 2.0, "steps": steps}
    row = {"id": "a", "source": "kaggle", "domain": "Data Science", "steps": 1, "repetition": 1,
           "outcome": "ok", "optimized": lane, "cold": {**lane, "total_s": 6.0}}  # fmt: skip
    results.write_text(json.dumps(row) + "\n")

    code = run("--data-dir", str(tmp_path), "eval", "report", "--results", str(results))

    out = capsys.readouterr().out
    assert code == 0
    assert "1 runs over 1 items" in out
    assert "speedup" in out


def test_eval_report_on_a_missing_file_explains_itself(tmp_path: Path, capsys):
    run("--data-dir", str(tmp_path), "eval", "report", "--results", str(tmp_path / "nope.jsonl"))
    assert "no results at" in capsys.readouterr().err


def test_eval_run_needs_a_token(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("READIE_EVAL_TOKEN", raising=False)
    run("--data-dir", str(tmp_path), "eval", "run", "--corpus", str(tmp_path / "c.json"))
    assert "READIE_EVAL_TOKEN" in capsys.readouterr().err


def test_eval_run_sends_each_item_to_the_server_and_records_the_answers(
    tmp_path: Path, capsys, monkeypatch
):
    import httpx

    corpus = tmp_path / "eval.json"
    corpus.write_text(json.dumps([item("pandas").to_json(), item("numpy").to_json()]))
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        lane = {"ok": True, "total_s": 1.0, "steps": [{"index": 0, "ok": True, "duration_s": 1.0}]}
        return httpx.Response(
            200, json={"lanes": {"optimized": lane, "cold": {**lane, "total_s": 2.0}}}
        )

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda: real(transport=httpx.MockTransport(handler)))

    code = run(
        "--data-dir", str(tmp_path), "eval", "run", "--corpus", str(corpus), "--token", "tok",
        "--repetitions", "2", "--memory", "4Gi", "--timeout", "60",
    )  # fmt: skip

    out = capsys.readouterr().out
    assert code == 0
    assert len(seen) == 4
    assert {r["memory"] for r in seen} == {"4Gi"}
    assert {r["timeout_s"] for r in seen} == {60.0}
    assert "ok: 4" in out
    results = tmp_path / "evals" / "eval.results.jsonl"
    assert len(results.read_text().splitlines()) == 4
