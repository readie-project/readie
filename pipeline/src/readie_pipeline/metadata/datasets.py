"""Measure Kaggle datasets as in-memory pandas resources."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlparse

import kagglehub  # type: ignore[import-untyped]
from kagglehub.exceptions import (  # type: ignore[import-untyped]
    BackendError,
    DataCorruptionError,
    KaggleApiHTTPError,
    KaggleEnvironmentError,
    NotFoundError,
    UnauthenticatedError,
)

from readie_pipeline.metadata.models import PackageFacts, ResourceType

_MB = 1024 * 1024
_DATASET_PATH_PARTS = 3
_OWNER_DATASET_PARTS = 2
_DEPENDENCIES = {"pandas": "(any)", "kagglehub": "(any)"}
_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_DELAYS = (30.0, 60.0, 120.0)


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

    @classmethod
    def unsupported_reference(cls, slug: str, kind: str) -> DatasetMeasurementError:
        """Describe a corpus reference outside the dataset API."""
        if kind == "competition":
            return cls(f"unsupported Kaggle competition reference: {slug}")
        return cls(f"malformed Kaggle dataset reference: {slug}")


class DatasetReferenceKind(StrEnum):
    """The kind of Kaggle resource named by a corpus reference."""

    DATASET = "dataset"
    COMPETITION = "competition"
    INVALID = "invalid"


def dataset_reference_kind(value: str) -> DatasetReferenceKind:
    """Classify a corpus reference before passing it to KaggleHub."""
    parsed = urlparse(value)
    path = parsed.path.strip("/") if parsed.scheme and parsed.netloc else value.strip("/")
    parts = [part for part in path.split("/") if part]

    if parts and parts[0] == "c":
        return DatasetReferenceKind.COMPETITION
    if parsed.scheme and parsed.netloc and parts and parts[0] == "datasets":
        return (
            DatasetReferenceKind.DATASET
            if len(parts) >= _DATASET_PATH_PARTS
            else DatasetReferenceKind.INVALID
        )
    if not parsed.scheme and len(parts) == _OWNER_DATASET_PARTS:
        return DatasetReferenceKind.DATASET
    if parsed.scheme and parsed.netloc and len(parts) == _OWNER_DATASET_PARTS:
        return DatasetReferenceKind.DATASET
    return DatasetReferenceKind.INVALID


def dataset_handle(value: str) -> str:
    """Convert a Kaggle dataset URL to the handle KaggleHub expects."""
    parsed = urlparse(value)
    path = parsed.path.strip("/") if parsed.scheme and parsed.netloc else value.strip("/")
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] == "datasets":
        if len(parts) >= _DATASET_PATH_PARTS:
            return f"{parts[1]}/{parts[2]}"
        return value
    if len(parts) == _OWNER_DATASET_PARTS and not parts[0].startswith("http"):
        return f"{parts[0]}/{parts[1]}"
    return value


class DatasetDownloader(Protocol):
    """The KaggleHub operations needed by the measurer."""

    def dataset_download(
        self,
        slug: str,
        path: str | None = None,
        *,
        force_download: bool | None = False,
    ) -> str | Path:
        """Return a local copy used to enumerate every dataset file."""

    def dataset_load(
        self,
        adapter: Any,
        handle: str,
        path: str,
        *,
        sql_query: str | None = None,
    ) -> Any:
        """Load one dataset file through KaggleHub's adapter."""


@dataclass(frozen=True, slots=True)
class DatasetMeasurement:
    """Measured dataset facts and the DataFrames produced during loading."""

    facts: PackageFacts
    frames: Mapping[str, Any]


def _kagglehub() -> DatasetDownloader:
    return cast(DatasetDownloader, kagglehub)


def _files(downloaded: str | Path) -> tuple[Path, list[Path]]:
    path = Path(downloaded)
    if path.is_file():
        return path.parent, [path]
    if not path.is_dir():
        raise DatasetMeasurementError.invalid_path(path)
    return path, sorted(item for item in path.rglob("*") if item.is_file())


def _load_frames(
    loader: DatasetDownloader,
    handle: str,
    root: Path,
    path: Path,
) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    adapter = cast(Any, kagglehub.KaggleDatasetAdapter.PANDAS)

    def load(sql_query: str | None = None) -> Any:
        try:
            return loader.dataset_load(
                adapter,
                handle,
                relative,
                sql_query=sql_query,
            )
        except DataCorruptionError:
            # KaggleHub can resume a stale partial file left in its cache. A
            # forced refresh repairs that file before the adapter reads it.
            loader.dataset_download(handle, relative, force_download=True)
            return loader.dataset_load(
                adapter,
                handle,
                relative,
                sql_query=sql_query,
            )

    if path.suffix.lower() not in {".sqlite", ".sqlite3", ".db", ".db3", ".s3db", ".dl3"}:
        loaded = load()
        if isinstance(loaded, dict):
            return {f"{relative}::{sheet}": frame for sheet, frame in loaded.items()}
        return {relative: loaded}

    connection = sqlite3.connect(path)
    try:
        tables = connection.execute(
            "select name from sqlite_master where type = 'table' order by name"
        ).fetchall()
    finally:
        connection.close()

    frames: dict[str, Any] = {}
    for (table_name,) in tables:
        quoted_name = table_name.replace('"', '""')
        frames[f"{relative}::{table_name}"] = load(
            sql_query=f'SELECT * FROM "{quoted_name}"',  # noqa: S608 - table names are discovered locally and quoted
        )
    return frames


def measure_dataset(
    slug: str,
    *,
    downloader: DatasetDownloader | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> DatasetMeasurement:
    """Enumerate and load every file in one Kaggle dataset.

    Downloading is used only to enumerate the complete dataset. Conversion is
    delegated to KaggleHub's pandas adapter, one file at a time. Download time
    is not included in ``import_time``.
    """
    for attempt in range(_RATE_LIMIT_RETRIES + 1):
        measurement = _measure_dataset_once(
            slug,
            downloader=downloader,
            clock=clock,
        )
        if "429" not in measurement.facts.error or attempt == _RATE_LIMIT_RETRIES:
            return measurement
        time.sleep(_RATE_LIMIT_DELAYS[attempt])
    raise AssertionError


def _measure_dataset_once(
    slug: str,
    *,
    downloader: DatasetDownloader | None,
    clock: Callable[[], float],
) -> DatasetMeasurement:
    """Measure once without retrying transient API rate limits."""
    try:
        reference_kind = dataset_reference_kind(slug)
        if reference_kind != DatasetReferenceKind.DATASET:
            raise DatasetMeasurementError.unsupported_reference(slug, reference_kind)

        if downloader is None:
            downloader = _kagglehub()

        downloaded = downloader.dataset_download(dataset_handle(slug))
        root, paths = _files(downloaded)
        if not paths:
            raise DatasetMeasurementError.empty_dataset(slug)

        frames: dict[str, Any] = {}
        disk_size = 0
        memory_size = 0
        start = clock()
        for path in paths:
            disk_size += path.stat().st_size
            loaded_frames = _load_frames(
                downloader,
                dataset_handle(slug),
                root,
                path,
            )
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
    except (
        OSError,
        TypeError,
        ValueError,
        AttributeError,
        RuntimeError,
        BackendError,
        DataCorruptionError,
        KaggleApiHTTPError,
        KaggleEnvironmentError,
        NotFoundError,
        UnauthenticatedError,
    ) as exc:
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
    on_progress: object = None,
) -> dict[str, PackageFacts]:
    """Measure each distinct slug and return dataset facts keyed by slug."""
    results: dict[str, PackageFacts] = {}
    for slug in sorted(set(slugs)):
        measurement = measure_dataset(slug, downloader=downloader)
        results[slug] = measurement.facts
        if callable(on_progress):
            on_progress(slug, measurement.facts)
    return results
