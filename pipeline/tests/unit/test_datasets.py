from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas  # type: ignore[import-untyped]

from readie_pipeline.metadata.datasets import measure_dataset, measure_datasets
from readie_pipeline.metadata.models import ResourceType


class FakeFrame:
    def __init__(self, memory_bytes: int) -> None:
        self._memory_bytes = memory_bytes

    def memory_usage(self, *, index: bool, deep: bool) -> FakeMemoryUsage:
        assert index
        assert deep
        return FakeMemoryUsage(self._memory_bytes)


class FakeMemoryUsage:
    def __init__(self, value: int) -> None:
        self._value = value

    def sum(self) -> int:
        return self._value


class FakePandas:
    def __init__(self, sizes: dict[str, int]) -> None:
        self.sizes = sizes
        self.loaded: list[str] = []

    def read_csv(self, path: Path) -> FakeFrame:
        self.loaded.append(path.name)
        return FakeFrame(self.sizes[path.name])


class FakeDownloader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.slugs: list[str] = []

    def dataset_download(self, slug: str) -> Path:
        self.slugs.append(slug)
        return self.path


class FailingDownloader:
    def dataset_download(self, slug: str) -> Path:
        msg = f"cannot download {slug}"
        raise RuntimeError(msg)


class FailingPandas(FakePandas):
    def read_csv(self, path: Path) -> FakeFrame:
        msg = f"cannot parse {path.name}"
        raise ValueError(msg)


def test_measure_dataset_aggregates_all_files_serially(tmp_path: Path) -> None:
    (tmp_path / "train.csv").write_bytes(b"a" * 3)
    (tmp_path / "test.csv").write_bytes(b"b" * 5)
    pandas = FakePandas({"train.csv": 10, "test.csv": 20})
    downloader = FakeDownloader(tmp_path)
    ticks = iter((10.0, 17.5))

    measurement = measure_dataset(
        "owner/data",
        downloader=downloader,
        pandas_module=pandas,
        clock=lambda: next(ticks),
    )

    assert downloader.slugs == ["owner/data"]
    assert pandas.loaded == ["test.csv", "train.csv"]
    assert measurement.facts.resource_type == ResourceType.DATASET
    assert measurement.facts.disk_size_mb == 8 / (1024 * 1024)
    assert measurement.facts.memory_size_mb == 30 / (1024 * 1024)
    assert measurement.facts.import_time == 7.5
    assert set(measurement.frames) == {str(tmp_path / "train.csv"), str(tmp_path / "test.csv")}


def test_measure_dataset_reports_unsupported_files(tmp_path: Path) -> None:
    (tmp_path / "README.txt").write_text("not tabular data")
    measurement = measure_dataset(
        "owner/data",
        downloader=FakeDownloader(tmp_path),
        pandas_module=FakePandas({}),
    )

    assert not measurement.facts.usable
    assert "unsupported dataset file type" in measurement.facts.error
    assert measurement.frames == {}


def test_measure_dataset_reports_an_unavailable_dataset() -> None:
    measurement = measure_dataset(
        "owner/data",
        downloader=FailingDownloader(),
        pandas_module=FakePandas({}),
    )

    assert not measurement.facts.usable
    assert "cannot download owner/data" in measurement.facts.error


def test_measure_dataset_reports_a_missing_download_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    measurement = measure_dataset(
        "owner/data",
        downloader=FakeDownloader(missing),
        pandas_module=FakePandas({}),
    )

    assert not measurement.facts.usable
    assert "no file or directory" in measurement.facts.error


def test_measure_dataset_reports_a_pandas_loading_error(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_bytes(b"not a valid frame")
    measurement = measure_dataset(
        "owner/data",
        downloader=FakeDownloader(tmp_path),
        pandas_module=FailingPandas({}),
    )

    assert not measurement.facts.usable
    assert "cannot parse data.csv" in measurement.facts.error


def test_measure_dataset_loads_each_sqlite_table_as_a_dataframe(tmp_path: Path) -> None:
    database = tmp_path / "database.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute("create table Iris (name text, value integer)")
        connection.executemany("insert into Iris values (?, ?)", [("a", 1), ("b", 2)])
        connection.commit()
    finally:
        connection.close()

    measurement = measure_dataset(
        "owner/data",
        downloader=FakeDownloader(database),
        pandas_module=pandas,
    )

    assert measurement.facts.usable
    assert list(measurement.frames) == [f"{database}::Iris"]
    assert measurement.frames[f"{database}::Iris"].shape == (2, 2)
    assert measurement.facts.disk_size_mb > 0
    assert measurement.facts.memory_size_mb > 0
    assert measurement.facts.import_time >= 0


def test_measure_datasets_deduplicates_and_reports_progress(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_bytes(b"x")
    seen: list[str] = []

    facts = measure_datasets(
        ["owner/data", "owner/data"],
        downloader=FakeDownloader(tmp_path),
        pandas_module=FakePandas({"data.csv": 1}),
        on_progress=lambda name, _facts: seen.append(name),
    )

    assert list(facts) == ["owner/data"]
    assert seen == ["owner/data"]
