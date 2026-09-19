"""What gets measured, as opposed to how it is measured.

The real profiler subprocess needs actual installed distributions and is
exercised by hand against a real environment, not in this suite; where a test
below needs ``analyze_package`` to return something, it fakes it.
"""

from __future__ import annotations

from readie_pipeline.metadata import analyze
from readie_pipeline.metadata.models import PackageFacts


def test_installed_packages_is_every_top_level_name_this_environment_provides(monkeypatch):
    # Not the corpus's imports: a checkpoint restores into the base image, so
    # that -- whatever is actually importable here -- is the universe to measure.
    monkeypatch.setattr(
        analyze,
        "packages_distributions",
        lambda: {"pandas": ["pandas"], "sklearn": ["scikit-learn"]},
    )
    assert analyze.installed_packages() == ["pandas", "sklearn"]


def test_installed_packages_is_sorted_so_a_run_diffs_cleanly(monkeypatch):
    monkeypatch.setattr(
        analyze, "packages_distributions", lambda: {"zlib": ["zlib"], "abc": ["abc"]}
    )
    assert analyze.installed_packages() == ["abc", "zlib"]


def test_installed_packages_is_empty_in_a_bare_environment(monkeypatch):
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    assert analyze.installed_packages() == []


def test_installed_packages_excludes_this_tool_itself(monkeypatch):
    # Analysing a real base image runs this tool's own interpreter with the
    # copied rootfs also on its PYTHONPATH (see pipeline/Dockerfile), so a
    # plain sys.path scan reports readie-pipeline itself as if it were part of
    # that base image -- confirmed against a real build, where it showed up
    # alongside pandas and torch as an "installed package."  It never is one.
    monkeypatch.setattr(
        analyze,
        "packages_distributions",
        lambda: {"pandas": ["pandas"], "readie_pipeline": ["readie-pipeline"]},
    )
    assert analyze.installed_packages() == ["pandas"]


def test_installed_packages_excludes_the_executor_harness(monkeypatch):
    # readie_executor genuinely is baked into every base image (the rootfs
    # stage in pipeline/Dockerfile installs it so a sandbox can run
    # `python -m readie_executor`) -- confirmed against a real build -- but no
    # request's own code ever imports the harness running it, so it is never
    # actionable for the planner and would just be permanent, unused clutter.
    monkeypatch.setattr(
        analyze,
        "packages_distributions",
        lambda: {"pandas": ["pandas"], "readie_executor": ["readie-executor"]},
    )
    assert analyze.installed_packages() == ["pandas"]


# ---------------------------------------------------------------------------
# Walking dependencies
# ---------------------------------------------------------------------------
def test_a_dependency_only_reached_by_a_wrong_name_guess_is_skipped_not_errored(monkeypatch):
    # top_level_imports() guesses an import name from the distribution name
    # when there is no top_level.txt to read -- true for many modern wheels
    # (Flask, beautifulsoup4, PyWavelets all lack one on a real Kaggle image)
    # -- and the guess is sometimes wrong ("Flask", not "flask"). Analysing a
    # real base image recorded 32 such wrong guesses as unmeasured "packages"
    # that never existed; a dependency name the guess cannot resolve must be
    # skipped instead, not recorded as failed.
    monkeypatch.setattr(analyze, "packages_distributions", lambda: {"pandas": ["pandas"]})
    monkeypatch.setattr(analyze, "top_level_imports", lambda dist: [dist])
    monkeypatch.setattr(
        analyze,
        "analyze_package",
        lambda import_name, _dist_name: PackageFacts(
            base_import=import_name, dependencies={"Flask": ">=1.0"}
        ),
    )

    metadata = analyze.analyze(["pandas"])

    assert "pandas" in metadata.packages
    assert "Flask" not in metadata.packages


def test_an_explicitly_requested_name_still_errors_when_nothing_provides_it(monkeypatch):
    # Unlike a guessed dependency name, a name the caller actually asked to
    # measure is recorded even when nothing provides it, so a later run can
    # tell "not measured" from "measured as free".
    monkeypatch.setattr(analyze, "packages_distributions", dict)

    metadata = analyze.analyze(["nonexistent"])

    assert (
        metadata.packages["nonexistent"].error == "no installed distribution provides this import"
    )


def test_on_progress_fires_even_when_nothing_provides_the_name(monkeypatch):
    # A package with no installed distribution at all used to return before
    # ever calling on_progress -- the one error category that is dropped from
    # the written metadata entirely (see Metadata.to_json) then had nowhere
    # left to surface at all, not even on the terminal.
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    seen: list[tuple[str, str]] = []

    metadata = analyze.analyze(
        ["nonexistent"], on_progress=lambda name, facts: seen.append((name, facts.error))
    )

    assert seen == [("nonexistent", "no installed distribution provides this import")]
    assert metadata.packages["nonexistent"].error
