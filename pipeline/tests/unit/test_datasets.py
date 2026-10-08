from __future__ import annotations

import sqlite3
from pathlib import Path

from kagglehub.exceptions import (  # type: ignore[import-untyped]
    DataCorruptionError,
    KaggleApiHTTPError,
)

from readie_pipeline.metadata.datasets import (
    DatasetMeasurement,
    DatasetReferenceKind,
    dataset_handle,
    dataset_reference_kind,
    measure_dataset,
    measure_datasets,
)
from readie_pipeline.metadata.models import PackageFacts, ResourceType


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


class FakeLoader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sizes: dict[str, int] = {}
        self.loaded: list[str] = []
        self.load_calls: list[tuple[str, str | None]] = []
        self.adapters: list[object] = []
        self.handles: list[str] = []
        self.slugs: list[str] = []

    def dataset_download(
        self,
        slug: str,
        path: str | None = None,
        *,
        force_download: bool | None = False,
    ) -> Path:
        del path, force_download
        self.slugs.append(slug)
        return self.path

    def dataset_load(
        self,
        adapter: object,
        handle: str,
        path: str,
        *,
        sql_query: str | None = None,
    ) -> FakeFrame:
        self.adapters.append(adapter)
        self.handles.append(handle)
        self.load_calls.append((path, sql_query))
        self.loaded.append(path)
        if Path(path).suffix.lower() not in {".csv", ".sqlite"}:
            message = f"unsupported dataset file type: {path}"
            raise ValueError(message)
        return FakeFrame(self.sizes[Path(path).name])


class FailingLoader:
    def dataset_download(
        self,
        slug: str,
        path: str | None = None,
        *,
        force_download: bool | None = False,
    ) -> Path:
        del slug, path, force_download
        raise RuntimeError("cannot download owner/data")

    def dataset_load(self, *args: object, **kwargs: object) -> FakeFrame:
        raise AssertionError("dataset_load should not be called")


class FailingLoad:
    def __init__(self, path: Path) -> None:
        self.path = path

    def dataset_download(
        self,
        slug: str,
        path: str | None = None,
        *,
        force_download: bool | None = False,
    ) -> Path:
        del slug, path, force_download
        return self.path

    def dataset_load(self, *args: object, **kwargs: object) -> FakeFrame:
        msg = "cannot load data.csv"
        raise RuntimeError(msg)


class FailingKaggleLoader:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def dataset_download(
        self,
        slug: str,
        path: str | None = None,
        *,
        force_download: bool | None = False,
    ) -> Path:
        del slug, path, force_download
        raise self.error

    def dataset_load(self, *args: object, **kwargs: object) -> FakeFrame:
        raise AssertionError("dataset_load should not be called")


class RetryingKaggleLoader(FakeLoader):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.force_downloads: list[tuple[str, str | None]] = []
        self._failed = False

    def dataset_download(
        self,
        slug: str,
        path: str | None = None,
        *,
        force_download: bool | None = False,
    ) -> Path:
        if force_download:
            self.force_downloads.append((slug, path))
        return super().dataset_download(slug)

    def dataset_load(
        self,
        adapter: object,
        handle: str,
        path: str,
        *,
        sql_query: str | None = None,
    ) -> FakeFrame:
        if not self._failed:
            self._failed = True
            raise DataCorruptionError("checksum mismatch")
        return super().dataset_load(adapter, handle, path, sql_query=sql_query)


def test_dataset_handle_extracts_a_kaggle_dataset_url() -> None:
    assert dataset_handle("https://www.kaggle.com/datasets/owner/name") == "owner/name"
    assert dataset_handle("https://www.kaggle.com/datasets/owner/name/data") == "owner/name"
    assert dataset_handle("https://www.kaggle.com/owner/name") == "owner/name"
    assert dataset_handle("owner/name") == "owner/name"


def test_dataset_handle_leaves_non_dataset_references_unchanged() -> None:
    value = "https://www.kaggle.com/c/competition/data"
    assert dataset_handle(value) == value
    ownerless = "https://www.kaggle.com/datasets/book-recommendation-dataset"
    assert dataset_handle(ownerless) == ownerless


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
        dataset_reference_kind("https://www.kaggle.com/datasets/name-only")
        == DatasetReferenceKind.INVALID
    )


def test_measure_dataset_rejects_non_dataset_references_before_download() -> None:
    measurement = measure_dataset("https://www.kaggle.com/c/digit-recognizer/data")

    assert not measurement.facts.usable
    assert "competition reference" in measurement.facts.error


def test_measure_dataset_reports_malformed_dataset_references() -> None:
    measurement = measure_dataset("https://www.kaggle.com/datasets/name-only")

    assert not measurement.facts.usable
    assert "malformed Kaggle dataset reference" in measurement.facts.error


def test_measure_dataset_aggregates_all_files_serially(tmp_path: Path) -> None:
    (tmp_path / "train.csv").write_bytes(b"a" * 3)
    (tmp_path / "test.csv").write_bytes(b"b" * 5)
    loader = FakeLoader(tmp_path)
    loader.sizes = {"train.csv": 10, "test.csv": 20}
    ticks = iter((10.0, 17.5))

    measurement = measure_dataset(
        "owner/data",
        downloader=loader,
        clock=lambda: next(ticks),
    )

    assert loader.slugs == ["owner/data"]
    assert loader.loaded == ["test.csv", "train.csv"]
    assert loader.handles == ["owner/data", "owner/data"]
    assert all(str(adapter).endswith("PANDAS") for adapter in loader.adapters)
    assert loader.load_calls == [("test.csv", None), ("train.csv", None)]
    assert measurement.facts.resource_type == ResourceType.DATASET
    assert measurement.facts.disk_size_mb == 8 / (1024 * 1024)
    assert measurement.facts.memory_size_mb == 30 / (1024 * 1024)
    assert measurement.facts.import_time == 7.5
    assert set(measurement.frames) == {"train.csv", "test.csv"}


def test_measure_dataset_reports_unsupported_files(tmp_path: Path) -> None:
    (tmp_path / "README.txt").write_text("not tabular data")
    measurement = measure_dataset(
        "owner/data",
        downloader=FakeLoader(tmp_path),
    )

    assert not measurement.facts.usable
    assert "unsupported dataset file type" in measurement.facts.error
    assert measurement.frames == {}


def test_measure_dataset_reports_an_unavailable_dataset() -> None:
    measurement = measure_dataset(
        "owner/data",
        downloader=FailingLoader(),
    )

    assert not measurement.facts.usable
    assert "cannot download owner/data" in measurement.facts.error


def test_measure_dataset_reports_a_missing_download_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    measurement = measure_dataset("owner/data", downloader=FakeLoader(missing))

    assert not measurement.facts.usable
    assert "no file or directory" in measurement.facts.error


def test_measure_dataset_reports_a_pandas_loading_error(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_bytes(b"not a valid frame")
    measurement = measure_dataset("owner/data", downloader=FailingLoad(tmp_path))

    assert not measurement.facts.usable
    assert "cannot load data.csv" in measurement.facts.error


def test_measure_dataset_loads_each_sqlite_table_as_a_dataframe(tmp_path: Path) -> None:
    database = tmp_path / "database.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute("create table Iris (name text, value integer)")
        connection.executemany("insert into Iris values (?, ?)", [("a", 1), ("b", 2)])
        connection.commit()
    finally:
        connection.close()

    loader = FakeLoader(database)
    loader.sizes = {"database.sqlite": 1}
    measurement = measure_dataset("owner/data", downloader=loader)

    assert measurement.facts.usable
    assert list(measurement.frames) == ["database.sqlite::Iris"]
    assert measurement.facts.disk_size_mb > 0
    assert measurement.facts.memory_size_mb > 0
    assert measurement.facts.import_time >= 0
    assert loader.loaded == ["database.sqlite"]
    assert loader.load_calls == [("database.sqlite", 'SELECT * FROM "Iris"')]


def test_measure_datasets_deduplicates_and_reports_progress(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_bytes(b"x")
    seen: list[str] = []

    loader = FakeLoader(tmp_path)
    loader.sizes = {"data.csv": 1}
    facts = measure_datasets(
        ["owner/data", "owner/data"],
        downloader=loader,
        on_progress=lambda name, _facts: seen.append(name),
    )

    assert list(facts) == ["owner/data"]
    assert seen == ["owner/data"]


def test_measure_datasets_retries_rate_limited_datasets(monkeypatch, tmp_path: Path) -> None:
    del tmp_path
    measurements = iter(
        (
            DatasetMeasurement(
                facts=PackageFacts(
                    base_import="owner/data",
                    error="could not measure dataset: 429 Client Error: Too Many Requests",
                    resource_type=ResourceType.DATASET,
                ),
                frames={},
            ),
            DatasetMeasurement(
                facts=PackageFacts(
                    base_import="owner/data",
                    resource_type=ResourceType.DATASET,
                ),
                frames={},
            ),
        )
    )

    def measure_once(*args: object, **kwargs: object) -> DatasetMeasurement:
        del args, kwargs
        return next(measurements)

    monkeypatch.setattr(
        "readie_pipeline.metadata.datasets._measure_dataset_once",
        measure_once,
    )
    monkeypatch.setattr("readie_pipeline.metadata.datasets.time.sleep", lambda _seconds: None)

    facts = measure_datasets(["owner/data"])

    assert facts["owner/data"].usable


def test_measure_dataset_turns_kaggle_http_errors_into_dataset_facts() -> None:
    measurement = measure_dataset(
        "owner/data",
        downloader=FailingKaggleLoader(KaggleApiHTTPError("403 forbidden")),
    )

    assert not measurement.facts.usable
    assert "403 forbidden" in measurement.facts.error


def test_measure_dataset_turns_kaggle_corruption_into_dataset_facts() -> None:
    measurement = measure_dataset(
        "owner/data",
        downloader=FailingKaggleLoader(DataCorruptionError("checksum mismatch")),
    )

    assert not measurement.facts.usable
    assert "checksum mismatch" in measurement.facts.error


def test_measure_dataset_refreshes_a_corrupted_file_before_loading(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_bytes(b"data")
    loader = RetryingKaggleLoader(tmp_path)
    loader.sizes = {"data.csv": 1}

    measurement = measure_dataset("owner/data", downloader=loader)

    assert measurement.facts.usable
    assert loader.force_downloads == [("owner/data", "data.csv")]
