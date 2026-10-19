"""The command line, end to end where it does not need runsc."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from readie_pipeline.cli import main
from readie_pipeline.metadata.models import Metadata, PackageFacts
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


def test_the_fixed_planner_is_selectable_and_plans_nothing_by_default(
    data_dir: Path, workspace: Path
):
    # The command line cannot name sets, so a fixed run from here has none: an empty plan,
    # no distributions to install, and `build` then captures no checkpoint.
    code = run(
        "--data-dir",
        str(data_dir),
        "--output-dir",
        str(workspace),
        "plan",
        "--no-spec",
        "--planner",
        "fixed",
    )

    assert code == 0
    assert json.loads((workspace / "checkpoints.json").read_text()) == []
    requirements = (workspace / "rootfs-requirements.txt").read_text().splitlines()
    assert all(line.startswith("#") for line in requirements)


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


def test_plan_lists_the_distributions_the_rootfs_must_install(
    data_dir: Path, workspace: Path, tmp_path: Path
):
    # The list decides what a minimal rootfs contains, so it is written by `plan`
    # (which runs before the image exists) beside the plan, for any planner.
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
    lines = (plans / "rootfs-requirements.txt").read_text().splitlines()
    assert lines[0].startswith("#")
    assert len(lines) > 1
    assert not (workspace / "rootfs-requirements.txt").exists()


def test_reuse_plan_keeps_the_existing_plan_and_does_not_plan_again(
    data_dir: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    # The capture container reuses the plan the host already made, so the rootfs
    # (built from that plan's distributions) and the captured checkpoints agree.
    common = ("--data-dir", str(data_dir), "--output-dir", str(workspace))
    assert run(*common, "plan", "--no-spec", "--max-checkpoints", "2") == 0
    before = {
        name: (workspace / name).read_bytes()
        for name in ("checkpoints.json", "rootfs-requirements.txt")
    }

    def refuse(*_args: object, **_kwargs: object) -> None:
        msg = "the planner ran"
        raise AssertionError(msg)

    monkeypatch.setattr("readie_pipeline.cli.build_planner", refuse)
    # Different settings: the file is the plan, so none of them may matter.
    code = run(*common, "plan", "--no-spec", "--reuse-plan", "--max-checkpoints", "1")

    assert code == 0
    assert "reusing the" in capsys.readouterr().out
    for name, content in before.items():
        assert (workspace / name).read_bytes() == content


def test_plan_records_what_the_image_build_could_not_install(
    data_dir: Path, workspace: Path, tmp_path: Path, capsys
):
    # In the capture container the rootfs is there and the plan directory is the host's,
    # so the person running the build can read the list without a `docker run`.
    bundle = tmp_path / "bundle"
    (bundle / "rootfs" / "etc").mkdir(parents=True)
    (bundle / "rootfs" / "etc" / "readie-rootfs-skipped.txt").write_text("scikit-learn\n")
    plans = tmp_path / "plan"

    code = run(
        "--data-dir", str(data_dir), "--output-dir", str(workspace), "--plan-dir", str(plans),
        "--bundle-dir", str(bundle), "plan", "--no-spec",
    )  # fmt: skip

    assert code == 0
    assert (plans / "rootfs-skipped.txt").read_text().splitlines()[-1] == "scikit-learn"
    assert "scikit-learn" in capsys.readouterr().out


def test_analyze_records_the_image_it_ran_in_and_the_versions_it_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    # `make analyze` passes the base image name in, because an image does not record its
    # own FROM, and the versions come from the environment that measured.
    image = "gcr.io/kaggle-images/python@sha256:" + "ab" * 32
    monkeypatch.setenv("READIE_BASE_IMAGE", image)
    monkeypatch.setattr("readie_pipeline.cli.installed_packages", lambda: ["pandas"])
    monkeypatch.setattr(
        "readie_pipeline.cli.analyze",
        lambda *_a, **_k: Metadata(
            {"pandas": PackageFacts(base_import="pandas", distribution="pandas")},
            distributions={"pandas": "2.3.3"},
        ),
    )

    code = run("--data-dir", str(tmp_path), "analyze")

    assert code == 0
    written = json.loads((tmp_path / "metadata" / "cpu.json").read_text())
    assert written["base_image"] == image
    assert written["distributions"] == {"pandas": "2.3.3"}
    assert image in capsys.readouterr().out


def test_analyze_without_a_named_image_writes_an_empty_base_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("READIE_BASE_IMAGE", raising=False)
    monkeypatch.setattr("readie_pipeline.cli.installed_packages", lambda: [])
    monkeypatch.setattr("readie_pipeline.cli.analyze", lambda *_a, **_k: Metadata({}))

    assert run("--data-dir", str(tmp_path), "analyze") == 0

    assert json.loads((tmp_path / "metadata" / "cpu.json").read_text())["base_image"] == ""


def test_reuse_plan_without_a_plan_explains_itself(data_dir: Path, workspace: Path, capsys):
    code = run(
        "--data-dir", str(data_dir), "--output-dir", str(workspace),
        "plan", "--no-spec", "--reuse-plan",
    )  # fmt: skip

    assert code == 1
    assert "to reuse" in capsys.readouterr().err


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
