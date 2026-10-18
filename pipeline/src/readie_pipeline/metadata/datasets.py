"""Measure Kaggle datasets as in-memory pandas resources."""

from __future__ import annotations

import sqlite3
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlparse

import kagglehub  # type: ignore[import-untyped]

from readie_pipeline.metadata.models import ResourceFacts, ResourceType

_MB = 1024 * 1024
_DATASET_PATH_PARTS = 3
_OWNER_DATASET_PARTS = 2
_FILE_LOAD_ERRORS = (OSError, TypeError, ValueError, AttributeError, RuntimeError)
_PANDAS_EXTENSIONS = frozenset(
    {
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".xml",
        ".parquet",
        ".feather",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".db3",
        ".s3db",
        ".dl3",
        ".xls",
        ".xlsx",
        ".xlsm",
        ".xlsb",
        ".odf",
        ".ods",
        ".odt",
    }
)
_SQLITE_EXTENSIONS = frozenset({".sqlite", ".sqlite3", ".db", ".db3", ".s3db", ".dl3"})


class ResourceMeasurementError(ValueError):
    """A resource could not be measured."""

    @classmethod
    def invalid_path(cls, path: Path) -> ResourceMeasurementError:
        """Describe a downloaded path that cannot be read."""
        return cls(f"resource path is not a file or directory: {path}")

    @classmethod
    def invalid_reference(cls, slug: str) -> ResourceMeasurementError:
        """Describe a corpus reference that is not a dataset."""
        return cls(f"resource reference is invalid: {slug}")

    @classmethod
    def empty_dataset(cls) -> ResourceMeasurementError:
        """Describe a dataset directory with no files."""
        return cls("resource contains no files")


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
    """The KaggleHub operations needed to inventory and load a dataset."""

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
    """Measured dataset facts and the variables produced during loading."""

    facts: ResourceFacts
    values: Mapping[str, Any]


def _kagglehub() -> DatasetDownloader:
    return cast(DatasetDownloader, kagglehub)


def _files(downloaded: str | Path) -> tuple[Path, list[Path]]:
    path = Path(downloaded)
    if path.is_file():
        return path.parent, [path]
    if not path.is_dir():
        raise ResourceMeasurementError.invalid_path(path)
    return path, sorted(item for item in path.rglob("*") if item.is_file())


def dependencies() -> tuple[str, ...]:
    """Return typed resources required by the dataset resource loader."""
    return ("package:pandas", "package:kagglehub")


def _load_values(
    loader: DatasetDownloader,
    handle: str,
    root: Path,
    path: Path,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    relative = path.relative_to(root).as_posix()
    if path.suffix.lower() not in _PANDAS_EXTENSIONS:
        return {relative: path.read_bytes()}, ()
    try:
        adapter = cast(Any, kagglehub.KaggleDatasetAdapter.PANDAS)
        if path.suffix.lower() in _SQLITE_EXTENSIONS:
            with sqlite3.connect(path) as connection:
                table_names = [
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                    )
                ]
            values = {}
            for table_name in table_names:
                escaped_name = table_name.replace('"', '""')
                values[f"{relative}::{table_name}"] = loader.dataset_load(
                    adapter,
                    handle,
                    relative,
                    sql_query=f'SELECT * FROM "{escaped_name}"',  # noqa: S608 - escaped SQLite identifier
                )
            return values, ()
        loaded = loader.dataset_load(adapter, handle, relative)
        if isinstance(loaded, dict):
            values = {f"{relative}::{sheet}": frame for sheet, frame in loaded.items()}
        else:
            values = {relative: loaded}
        return values, ()  # noqa: TRY300 - both result shapes share one exception handler
    except _FILE_LOAD_ERRORS:
        return {}, (relative,)


def measure_dataset(
    slug: str,
    *,
    downloader: DatasetDownloader | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> DatasetMeasurement:
    """Enumerate and load every file in one Kaggle dataset sequentially.

    Downloading provides the complete inventory. CSV and JSON files are loaded
    through KaggleHub's pandas adapter; unsupported or failed files are retained
    as raw bytes. Download time is excluded from ``import_time``.
    """
    return _measure_dataset_once(slug, downloader=downloader, clock=clock)


def _failed_measurement(slug: str) -> DatasetMeasurement:
    return DatasetMeasurement(
        facts=ResourceFacts(
            base_import=slug,
            resource_type=ResourceType.KAGGLE_DATASET,
            dependencies=frozenset(dependencies()),
            error="resource could not be loaded",
        ),
        values={},
    )


def _log(slug: str, message: str) -> None:
    print(f"    dataset {slug}: {message}", file=sys.stderr)


def _measure_dataset_once(  # noqa: PLR0912 - each measurement decision is logged here
    slug: str,
    *,
    downloader: DatasetDownloader | None,
    clock: Callable[[], float],
) -> DatasetMeasurement:
    """Measure one sequential pass over the downloaded dataset."""
    try:
        reference_kind = dataset_reference_kind(slug)
        if reference_kind == DatasetReferenceKind.COMPETITION:
            _log(slug, "skipped (competition URL)")
            return _failed_measurement(slug)
        if reference_kind == DatasetReferenceKind.INVALID:
            _log(slug, "skipped (invalid dataset reference)")
            return _failed_measurement(slug)

        if downloader is None:
            downloader = _kagglehub()

        handle = dataset_handle(slug)
        try:
            downloaded = downloader.dataset_download(handle)
        except Exception as exc:  # noqa: BLE001 - external resource failures are expected
            _log(slug, f"failed to download ({type(exc).__name__})")
            return _failed_measurement(slug)

        root, paths = _files(downloaded)
        if not paths:
            _log(slug, "failed (download contained no files)")
            return _failed_measurement(slug)

        values: dict[str, Any] = {}
        file_errors: list[str] = []
        disk_size = 0
        memory_size = 0
        start = clock()
        for path in paths:
            relative = path.relative_to(root).as_posix()
            try:
                disk_size += path.stat().st_size
            except OSError:
                file_errors.append(relative)
                continue
            loaded_values, errors = _load_values(
                downloader,
                handle,
                root,
                path,
            )
            values.update(loaded_values)
            file_errors.extend(errors)
            for value_name, value in loaded_values.items():
                try:
                    if isinstance(value, bytes):
                        memory_size += len(value)
                    else:
                        memory_size += int(value.memory_usage(index=True, deep=True).sum())
                except _FILE_LOAD_ERRORS:
                    values.pop(value_name)
                    file_errors.append(value_name)
        load_time = clock() - start

        facts = ResourceFacts(
            base_import=slug,
            disk_size_mb=disk_size / _MB,
            memory_size_mb=memory_size / _MB,
            import_time=load_time,
            resource_type=ResourceType.KAGGLE_DATASET,
            dependencies=frozenset(dependencies()),
            error="resource could not be loaded" if file_errors else "",
        )
        if file_errors:
            _log(
                slug,
                f"measured with {len(values)} loaded value(s); "
                f"{len(file_errors)} file(s) could not be loaded",
            )
        else:
            _log(slug, f"measured successfully ({len(values)} loaded value(s))")
        return DatasetMeasurement(facts=facts, values=values)
    except Exception as exc:  # noqa: BLE001 - external resource failures become one generic state
        _log(slug, f"failed during measurement ({type(exc).__name__})")
        return _failed_measurement(slug)


def measure_datasets(
    slugs: Sequence[str],
    *,
    downloader: DatasetDownloader | None = None,
    on_progress: object = None,
) -> dict[str, ResourceFacts]:
    """Measure each distinct slug and return dataset facts keyed by slug."""
    results: dict[str, ResourceFacts] = {}
    for slug in sorted(set(slugs)):
        bare_slug = slug.removeprefix(f"{ResourceType.KAGGLE_DATASET}:")
        measurement = measure_dataset(bare_slug, downloader=downloader)
        results[f"{ResourceType.KAGGLE_DATASET}:{bare_slug}"] = measurement.facts
        if callable(on_progress):
            on_progress(bare_slug, measurement.facts)
    return results
