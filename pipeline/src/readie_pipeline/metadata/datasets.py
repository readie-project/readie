"""Measure Kaggle datasets as in-memory pandas resources."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import kagglehub  # type: ignore[import-untyped]
import pandas  # type: ignore[import-untyped]

from readie_pipeline.metadata.models import PackageFacts, ResourceType

_MB = 1024 * 1024
_DEPENDENCIES = {"pandas": "(any)", "kagglehub": "(any)"}
_READERS = {
    ".csv": "read_csv",
    ".json": "read_json",
    ".jsonl": "read_json",
    ".parquet": "read_parquet",
}


class DatasetMeasurementError(ValueError):
    """The downloaded dataset cannot be measured."""

    @classmethod
    def invalid_path(cls, path: Path) -> DatasetMeasurementError:
        """Describe a KaggleHub path that cannot be read."""
        return cls(f"KaggleHub returned no file or directory: {path}")

    @classmethod
    def unsupported_type(cls, path: Path) -> DatasetMeasurementError:
        """Describe a file format without a configured pandas reader."""
        return cls(f"unsupported dataset file type: {path.name}")

    @classmethod
    def empty_dataset(cls, slug: str) -> DatasetMeasurementError:
        """Describe a dataset directory with no files."""
        return cls(f"KaggleHub dataset contains no files: {slug}")


class DatasetDownloader(Protocol):
    """The KaggleHub operation needed by the measurer."""

    def dataset_download(self, slug: str) -> str | Path:
        """Return a downloaded dataset file or directory."""


@dataclass(frozen=True, slots=True)
class DatasetMeasurement:
    """Measured dataset facts and the DataFrames produced during loading."""

    facts: PackageFacts
    frames: Mapping[str, Any]


def _kagglehub() -> DatasetDownloader:
    return cast(DatasetDownloader, kagglehub)


def _files(downloaded: str | Path) -> list[Path]:
    path = Path(downloaded)
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise DatasetMeasurementError.invalid_path(path)
    return sorted(item for item in path.rglob("*") if item.is_file())


def _load_frames(pandas: Any, path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in {".sqlite", ".db", ".sqlite3"}:
        connection = sqlite3.connect(path)
        try:
            tables = connection.execute(
                "select name from sqlite_master where type = 'table' order by name"
            ).fetchall()
            frames: dict[str, Any] = {}
            for (table_name,) in tables:
                quoted_name = table_name.replace('"', '""')
                cursor = connection.execute(f'SELECT * FROM "{quoted_name}"')  # noqa: S608 - table names are discovered locally and quoted
                columns = [column[0] for column in cursor.description or ()]
                frames[f"{path}::{table_name}"] = pandas.DataFrame.from_records(
                    cursor.fetchall(),
                    columns=columns,
                )
            return frames
        finally:
            connection.close()

    reader_name = _READERS.get(suffix)
    if reader_name is None:
        raise DatasetMeasurementError.unsupported_type(path)
    reader: Callable[..., Any] = getattr(pandas, reader_name)
    kwargs: dict[str, Any] = {}
    if suffix == ".jsonl":
        kwargs["lines"] = True
    return {str(path): reader(path, **kwargs)}


def measure_dataset(
    slug: str,
    *,
    downloader: DatasetDownloader | None = None,
    pandas_module: Any | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> DatasetMeasurement:
    """Download and measure every supported file in one Kaggle dataset.

    Files are loaded serially into separate DataFrames. Download time is not
    included in ``import_time``; it measures local file-to-DataFrame loading.
    """
    try:
        if downloader is None:
            downloader = _kagglehub()
        if pandas_module is None:
            pandas_module = pandas

        downloaded = downloader.dataset_download(slug)
        paths = _files(downloaded)
        if not paths:
            raise DatasetMeasurementError.empty_dataset(slug)

        frames: dict[str, Any] = {}
        disk_size = 0
        memory_size = 0
        start = clock()
        for path in paths:
            disk_size += path.stat().st_size
            loaded_frames = _load_frames(pandas_module, path)
            frames.update(loaded_frames)
            memory_size += sum(
                int(frame.memory_usage(index=True, deep=True).sum())
                for frame in loaded_frames.values()
            )
        load_time = clock() - start

        facts = PackageFacts(
            base_import=slug,
            dependencies=dict(_DEPENDENCIES),
            disk_size_mb=disk_size / _MB,
            memory_size_mb=memory_size / _MB,
            import_time=load_time,
            resource_type=ResourceType.DATASET,
        )
        return DatasetMeasurement(facts=facts, frames=frames)
    except (OSError, TypeError, ValueError, AttributeError, RuntimeError) as exc:
        return DatasetMeasurement(
            facts=PackageFacts(
                base_import=slug,
                dependencies=dict(_DEPENDENCIES),
                resource_type=ResourceType.DATASET,
                error=f"could not measure dataset: {exc}",
            ),
            frames={},
        )


def measure_datasets(
    slugs: Sequence[str],
    *,
    downloader: DatasetDownloader | None = None,
    pandas_module: Any | None = None,
    on_progress: object = None,
) -> dict[str, PackageFacts]:
    """Measure each distinct slug and return dataset facts keyed by slug."""
    results: dict[str, PackageFacts] = {}
    for slug in sorted(set(slugs)):
        measurement = measure_dataset(
            slug,
            downloader=downloader,
            pandas_module=pandas_module,
        )
        results[slug] = measurement.facts
        if callable(on_progress):
            on_progress(slug, measurement.facts)
    return results
