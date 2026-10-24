from __future__ import annotations

from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]
import pytest

from readie_pipeline.metadata.datasets import (
    DatasetReferenceKind,
    dataset_handle,
    dataset_reference_kind,
    dependencies,
    measure_dataset,
    measure_datasets,
)
from readie_pipeline.metadata.models import ResourceType


class FakeDownloader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handles: list[str] = []

    def dataset_download(
        self,
        handle: str,
        path: str | None = None,
        *,
        force_download: bool | None = False,
    ) -> Path:
        del path, force_download
        self.handles.append(handle)
        return self.path

    def dataset_load(
        self,
        _adapter: object,
        _handle: str,
        path: str,
        *,
        sql_query: str | None = None,
    ) -> object:
        del sql_query
        file_path = self.path / path
        if file_path.suffix == ".csv":
            return pd.read_csv(file_path)
        if file_path.suffix == ".json":
            return pd.read_json(file_path)
        msg = f"unsupported test file: {path}"
        raise ValueError(msg)


class FailingDownloader:
    def dataset_download(
        self,
        handle: str,
        path: str | None = None,
        *,
        force_download: bool | None = False,
    ) -> Path:
        del handle, path, force_download
        raise RuntimeError("download failed")

    def dataset_load(
        self,
        _adapter: object,
        _handle: str,
        _path: str,
        *,
        sql_query: str | None = None,
    ) -> object:
        del sql_query
        raise AssertionError("dataset_load must not run after download failure")


def test_dataset_handle_extracts_a_kaggle_dataset_url() -> None:
    assert dataset_handle("https://www.kaggle.com/datasets/owner/name") == "owner/name"
    assert dataset_handle("https://www.kaggle.com/datasets/owner/name/data") == "owner/name"
    assert dataset_handle("https://www.kaggle.com/owner/name") == "owner/name"
    assert dataset_handle("owner/name") == "owner/name"


def test_dataset_reference_kind_distinguishes_supported_and_unsupported_entries() -> None:
    assert dataset_reference_kind("owner/name") == DatasetReferenceKind.DATASET
    assert (
        dataset_reference_kind("https://www.kaggle.com/datasets/owner/name/data")
        == DatasetReferenceKind.DATASET
    )
    assert (
        dataset_reference_kind("https://www.kaggle.com/c/digit-recognizer/data")
        == DatasetReferenceKind.COMPETITION
    )
    assert (
        dataset_reference_kind("https://www.kaggle.com/competitions/titanic/data")
        == DatasetReferenceKind.COMPETITION
    )
    assert (
        dataset_reference_kind("https://www.kaggle.com/datasets/name-only")
        == DatasetReferenceKind.INVALID
    )


def test_dataset_dependencies_are_static() -> None:
    assert dependencies() == ("package:pandas", "package:kagglehub")


def test_measure_dataset_uses_kagglehub_pandas_adapter(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("name,value\niris,1\n")

    measurement = measure_dataset("owner/data", downloader=FakeDownloader(tmp_path))

    assert measurement.values["data.csv"].to_dict("records") == [{"name": "iris", "value": 1}]


def test_measure_dataset_keeps_unsupported_file_types_as_bytes(tmp_path: Path) -> None:
    path = tmp_path / "model.bin"
    path.write_bytes(b"\x00\x01\x02")

    measurement = measure_dataset("owner/data", downloader=FakeDownloader(tmp_path))

    assert measurement.values["model.bin"] == b"\x00\x01\x02"


def test_measure_dataset_loads_every_file_in_sorted_order(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "z.csv").write_text("value\n2\n")
    (tmp_path / "nested" / "a.json").write_text('[{"value": 1}]')
    (tmp_path / "model.bin").write_bytes(b"\x00\x01")
    downloader = FakeDownloader(tmp_path)
    ticks = iter((10.0, 12.5))

    measurement = measure_dataset(
        "owner/data",
        downloader=downloader,
        clock=lambda: next(ticks),
    )

    assert downloader.handles == ["owner/data"]
    assert list(measurement.values) == ["model.bin", "nested/a.json", "z.csv"]
    assert isinstance(measurement.values["nested/a.json"], pd.DataFrame)
    assert measurement.values["model.bin"] == b"\x00\x01"
    assert measurement.facts.resource_type == ResourceType.KAGGLE_DATASET
    assert measurement.facts.disk_size_mb > 0
    assert measurement.facts.memory_size_mb > 0
    assert measurement.facts.import_time == 2.5
    assert measurement.facts.usable


def test_unsupported_files_are_retained_without_dataset_specific_parsing(tmp_path: Path) -> None:
    path = tmp_path / "model.bin"
    path.write_bytes(b"binary contents")

    measurement = measure_dataset("owner/data", downloader=FakeDownloader(tmp_path))

    assert measurement.values["model.bin"] == b"binary contents"
    assert measurement.facts.usable


def test_one_invalid_file_does_not_discard_other_files(tmp_path: Path) -> None:
    (tmp_path / "valid.csv").write_text("value\n1\n")
    (tmp_path / "invalid.json").write_text("{not json")

    measurement = measure_dataset("owner/data", downloader=FakeDownloader(tmp_path))

    assert set(measurement.values) == {"valid.csv"}
    assert measurement.facts.error == "resource could not be loaded"
    assert not measurement.facts.usable


def test_one_file_stat_failure_does_not_discard_other_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    valid = tmp_path / "valid.csv"
    failed = tmp_path / "failed.csv"
    valid.write_text("value\n1\n")
    failed.write_text("value\n2\n")
    monkeypatch.setattr(
        "readie_pipeline.metadata.datasets._files",
        lambda _downloaded: (tmp_path, [failed, valid]),
    )
    original_stat = Path.stat

    def stat(path: Path, *, follow_symlinks: bool = True):
        if path == failed:
            raise OSError("stat failed")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat)
    measurement = measure_dataset("owner/data", downloader=FakeDownloader(tmp_path))

    assert set(measurement.values) == {"valid.csv"}
    assert measurement.facts.error == "resource could not be loaded"


def test_competition_reference_is_skipped_and_logged(capsys: pytest.CaptureFixture[str]) -> None:
    measurement = measure_dataset("https://www.kaggle.com/c/competition/data")

    assert measurement.facts.error == "resource could not be loaded"
    assert not measurement.facts.usable
    assert "skipped (competition URL)" in capsys.readouterr().err


def test_invalid_reference_is_skipped_and_logged(capsys: pytest.CaptureFixture[str]) -> None:
    measurement = measure_dataset("https://www.kaggle.com/datasets/name-only")

    assert measurement.facts.error == "resource could not be loaded"
    assert not measurement.facts.usable
    assert "skipped (invalid dataset reference)" in capsys.readouterr().err


def test_download_failure_is_logged(capsys: pytest.CaptureFixture[str]) -> None:
    measurement = measure_dataset("owner/data", downloader=FailingDownloader())

    assert measurement.facts.error == "resource could not be loaded"
    assert not measurement.facts.usable
    assert "failed to download (RuntimeError)" in capsys.readouterr().err


def test_measure_datasets_deduplicates_and_reports_progress(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_text("value\n1\n")
    seen: list[str] = []
    downloader = FakeDownloader(tmp_path)

    facts = measure_datasets(
        ["owner/data", "owner/data"],
        downloader=downloader,
        on_progress=lambda name, _facts: seen.append(name),
    )

    assert list(facts) == ["kaggle_dataset:owner/data"]
    assert seen == ["owner/data"]
    assert downloader.handles == ["owner/data"]


def test_measure_datasets_accepts_typed_corpus_keys(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_text("value\n1\n")
    downloader = FakeDownloader(tmp_path)

    facts = measure_datasets(
        ["kaggle_dataset:owner/data"],
        downloader=downloader,
    )

    assert list(facts) == ["kaggle_dataset:owner/data"]
    assert downloader.handles == ["owner/data"]
