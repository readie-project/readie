from __future__ import annotations

import json
from pathlib import Path

import pytest

from readie_pipeline.config import ConfigError
from readie_pipeline.metadata.analyze import analyze
from readie_pipeline.metadata.models import Metadata, ResourceFacts
from readie_pipeline.metadata.progress import ProgressJournal, read_progress
from readie_pipeline.resources.cleanup import CACHE_MARKER, CACHE_MARKER_CONTENT, cache_cleanup
from readie_pipeline.resources.identifiers import ResourceType
from readie_pipeline.resources.packages import ColdProfile


@pytest.fixture(autouse=True)
def dedicated_cache_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("KAGGLEHUB_CACHE", str(tmp_path / "cache"))


def marked_cache(tmp_path: Path) -> Path:
    root = tmp_path / "cache"
    root.mkdir()
    (root / CACHE_MARKER).write_text(CACHE_MARKER_CONTENT)
    return root


def test_cleanup_removes_only_the_completed_dataset(tmp_path: Path) -> None:
    root = marked_cache(tmp_path)
    finished = root / "datasets/owner/done/versions/1"
    pending = root / "datasets/owner/pending/versions/1"
    for folder in (finished, pending):
        folder.mkdir(parents=True)
        (folder / "data.csv").write_text("value\n1\n")
    release = cache_cleanup(root)
    release("kaggle_dataset:owner/done")
    assert not (root / "datasets/owner/done").exists()
    assert (pending / "data.csv").exists()
    assert (root / CACHE_MARKER).exists()
    release("package:pandas")
    release("kaggle_dataset:owner/done")


def test_cleanup_refuses_an_unmarked_cache(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="dedicated profiling cache"):
        cache_cleanup(tmp_path)


def test_cleanup_refuses_a_different_download_directory(tmp_path: Path, monkeypatch) -> None:
    root = marked_cache(tmp_path)
    monkeypatch.setenv("KAGGLEHUB_CACHE", str(tmp_path / "other"))
    with pytest.raises(ConfigError, match="KAGGLEHUB_CACHE"):
        cache_cleanup(root)


def test_rejected_dataset_references_have_no_cache_to_remove(tmp_path: Path) -> None:
    root = marked_cache(tmp_path)
    cache_cleanup(root)("kaggle_dataset:https://www.kaggle.com/c/competition/data")
    assert (root / CACHE_MARKER).exists()


def test_cleanup_refuses_a_symlink_outside_the_cache(tmp_path: Path) -> None:
    root = marked_cache(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "important").write_text("preserve")
    (root / "datasets").mkdir()
    (root / "datasets/owner").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigError, match="outside"):
        cache_cleanup(root)("kaggle_dataset:owner/data")
    assert (outside / "important").read_text() == "preserve"


class NoDrop:
    def drop(self):
        pass


class Handler:
    def __init__(self, error=""):
        self.error = error

    def requires(self, identifier):
        return ()

    def analyze(self, identifier, context):
        return Metadata(
            {
                identifier: ResourceFacts(
                    base_import="owner/data",
                    resource_type=ResourceType.KAGGLE_DATASET,
                    error=self.error,
                )
            }
        )


def test_cleanup_happens_only_after_the_success_is_journaled(tmp_path: Path) -> None:
    path = tmp_path / "partial"
    journal = ProgressJournal(path, samples=3, base_image="image", installed={}, resume=False)
    released: list[str] = []

    def release(identifier):
        saved, samples = read_progress(path)
        assert identifier in saved.resources
        assert samples == 3
        released.append(identifier)

    identifier = "kaggle_dataset:owner/data"
    try:
        analyze(
            [identifier],
            cold=ColdProfile(NoDrop(), 3),
            analyzers={ResourceType.KAGGLE_DATASET: Handler()},
            journal=journal,
            after_persist=release,
        )
    finally:
        journal.close()
    assert released == [identifier]


def test_failed_measurements_are_cleaned_only_after_diagnostics_are_persisted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "partial"
    journal = ProgressJournal(path, samples=3, base_image="image", installed={}, resume=False)
    released: list[str] = []

    def release(identifier):
        record = json.loads(path.with_name(path.name + ".failures.jsonl").read_text())
        assert record["failure"] == identifier
        assert record["error"] == "failed"
        assert read_progress(path)[0].resources == {}
        released.append(identifier)

    try:
        analyze(
            ["kaggle_dataset:owner/data"],
            cold=ColdProfile(NoDrop(), 3),
            analyzers={ResourceType.KAGGLE_DATASET: Handler("failed")},
            journal=journal,
            after_persist=release,
        )
    finally:
        journal.close()
    assert released == ["kaggle_dataset:owner/data"]
    released.clear()
    analyze(
        ["kaggle_dataset:owner/data"],
        cold=ColdProfile(NoDrop(), 3),
        analyzers={ResourceType.KAGGLE_DATASET: Handler()},
        after_persist=released.append,
    )
    assert released == []


@pytest.mark.parametrize("error", ["", "failed"])
def test_journal_failure_keeps_downloads_available(error):
    class BrokenJournal:
        def resource(self, name, facts):
            raise OSError("disk full")

        def resolution(self, raw, resolved):
            pass

    released: list[str] = []
    with pytest.raises(OSError, match="disk full"):
        analyze(
            ["kaggle_dataset:owner/data"],
            cold=ColdProfile(NoDrop(), 3),
            analyzers={ResourceType.KAGGLE_DATASET: Handler(error)},
            journal=BrokenJournal(),
            after_persist=released.append,
        )
    assert released == []
