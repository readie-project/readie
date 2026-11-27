"""Measure Kaggle datasets as in-memory pandas resources."""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlparse

import kagglehub  # type: ignore[import-untyped]

from readie_pipeline.config import Settings
from readie_pipeline.metadata.cache import KernelCacheDropper
from readie_pipeline.metadata.models import ResourceFacts
from readie_pipeline.resources.identifiers import ResourceType

_MB = 1024 * 1024
_DATASET_PATH_PARTS = 3
_OWNER_DATASET_PARTS = 2
_FILE_LOAD_ERRORS = (
    OSError,
    TypeError,
    ValueError,
    SyntaxError,
    AttributeError,
    RuntimeError,
    ImportError,
    sqlite3.Error,
)
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

    if parts and parts[0] in {"c", "competitions"}:
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
    """Download a dataset once and load files from its local inventory."""

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
        """Load one inventoried file without another download."""


@dataclass(frozen=True, slots=True)
class DatasetMeasurement:
    """Measured dataset facts and the variables produced during loading."""

    facts: ResourceFacts
    values: Mapping[str, Any]


class KaggleDatasetLoader:
    """Use KaggleHub for inventory and pandas for reads from the extracted files."""

    def __init__(self) -> None:
        self._root: Path | None = None

    def dataset_download(
        self,
        slug: str,
        path: str | None = None,
        *,
        force_download: bool | None = False,
    ) -> str | Path:
        """Download the inventory before any timed reads."""
        downloaded = kagglehub.dataset_download(slug, path, force_download=force_download)
        root = Path(downloaded)
        self._root = root.parent if root.is_file() else root
        return cast(str | Path, downloaded)

    def dataset_load(
        self,
        adapter: Any,
        handle: str,
        path: str,
        *,
        sql_query: str | None = None,
    ) -> Any:
        """Read an extracted file locally; KaggleHub's adapter would download it again."""
        del adapter, handle
        import pandas as pd  # type: ignore[import-untyped]  # noqa: PLC0415 - timed dependency

        if self._root is None:
            msg = "download the dataset inventory before loading a file"
            raise ValueError(msg)
        local = self._root / path
        extension = local.suffix.lower()
        if extension in _SQLITE_EXTENSIONS:
            with contextlib.closing(sqlite3.connect(local)) as connection:
                return pd.read_sql_query(sql_query, connection)
        if extension in {".csv", ".tsv"}:
            return pd.read_csv(local, sep="\t" if extension == ".tsv" else ",")
        if extension in {".json", ".jsonl"}:
            return pd.read_json(local, lines=extension == ".jsonl")
        readers = {
            ".xml": pd.read_xml,
            ".parquet": pd.read_parquet,
            ".feather": pd.read_feather,
        }
        if extension in readers:
            return readers[extension](local)
        return pd.read_excel(local)


def _kagglehub() -> DatasetDownloader:
    return KaggleDatasetLoader()


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


def _prepare_dependencies() -> None:
    """Import the loader's package prerequisites outside the timed operation."""
    for identifier in dependencies():
        importlib.import_module(identifier.partition(":")[2])


def _load_values(
    loader: DatasetDownloader,
    handle: str,
    root: Path,
    path: Path,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    relative = path.relative_to(root).as_posix()
    if path.suffix.lower() not in _PANDAS_EXTENSIONS:
        try:
            return {relative: path.read_bytes()}, ()
        except OSError:
            return {}, (relative,)
    try:
        adapter = cast(Any, kagglehub.KaggleDatasetAdapter.PANDAS)
        if path.suffix.lower() in _SQLITE_EXTENSIONS:
            with contextlib.closing(sqlite3.connect(path)) as connection:
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
        try:
            return {relative: path.read_bytes()}, ()
        except OSError:
            return {}, (relative,)


def measure_dataset(
    slug: str,
    *,
    downloader: DatasetDownloader | None = None,
    clock: Callable[[], float] = time.perf_counter,
    before_load: Callable[[], None] | None = None,
) -> DatasetMeasurement:
    """Enumerate and load every file in one Kaggle dataset sequentially.

    Downloading provides the complete inventory. CSV and JSON files are loaded
    locally through pandas; unsupported or failed files are retained
    as raw bytes. Download time is excluded from ``import_time``.
    """
    return _measure_dataset_once(slug, downloader=downloader, clock=clock, before_load=before_load)


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
    before_load: Callable[[], None] | None,
) -> DatasetMeasurement:
    """Measure one sequential pass over the downloaded dataset."""
    try:
        reference_kind = dataset_reference_kind(slug)
        if reference_kind != DatasetReferenceKind.DATASET:
            reason = (
                "competition URL"
                if reference_kind == DatasetReferenceKind.COMPETITION
                else "invalid dataset reference"
            )
            _log(slug, f"skipped ({reason})")
            return _failed_measurement(slug)

        if downloader is None:
            downloader = _kagglehub()

        handle = dataset_handle(slug)
        try:
            downloaded = downloader.dataset_download(handle)
        except Exception as exc:  # noqa: BLE001 - external resource failures are expected
            _log(slug, f"failed to download ({type(exc).__name__}): {exc}")
            return _failed_measurement(slug)

        root, paths = _files(downloaded)
        if not paths:
            _log(slug, "failed (download contained no files)")
            return _failed_measurement(slug)

        values: dict[str, Any] = {}
        file_errors: list[str] = []
        disk_size = 0
        memory_size = 0
        before_modules = set(sys.modules)
        # Resource timings exclude declared package dependencies, as package
        # timings do. Import them before clearing caches and starting the clock.
        _prepare_dependencies()
        if before_load is not None:
            before_load()
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
            dependencies=frozenset(dependencies())
            | frozenset(f"package:{name}" for name in set(sys.modules) - before_modules),
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
        _log(slug, f"failed during measurement ({type(exc).__name__}): {exc}")
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


@contextlib.contextmanager
def _measurement_output() -> Iterator[None]:
    """Keep library output off the result channel, including existing log handlers."""
    stdout_fd = sys.stdout.fileno()
    sys.stdout.flush()
    saved_stdout = os.dup(stdout_fd)
    try:
        # KaggleHub installs a handler before this entry point runs. Replacing
        # sys.stdout alone does not redirect the stream that handler retained.
        os.dup2(sys.stderr.fileno(), stdout_fd)
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        try:
            sys.stdout.flush()
        finally:
            os.dup2(saved_stdout, stdout_fd)
            os.close(saved_stdout)


def _main() -> None:
    """Emit one dataset measurement for the isolated analysis handler."""
    with _measurement_output():
        before_load = None
        if "--cold" in sys.argv[2:]:
            dropper = KernelCacheDropper(Settings.from_env().drop_caches_file)
            before_load = dropper.drop
        result = measure_dataset(sys.argv[1], before_load=before_load)
    payload = result.facts.to_json()
    payload["error"] = result.facts.error
    print(json.dumps(payload))


if __name__ == "__main__":
    _main()
