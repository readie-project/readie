"""Release source caches only after their measurements have been persisted."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from kagglehub.config import get_cache_folder  # type: ignore[import-untyped]

from readie_pipeline.config import ConfigError
from readie_pipeline.resources.identifiers import ResourceType, parse_resource
from readie_pipeline.resources.kaggle_dataset import (
    DatasetReferenceKind,
    dataset_handle,
    dataset_reference_kind,
)

CACHE_MARKER = ".readie-profile-cache"
CACHE_MARKER_CONTENT = "readie-profile-cache-v1\n"
_HANDLE_PARTS = 2


def _kaggle_dataset(root: Path, value: str) -> None:
    if dataset_reference_kind(value) != DatasetReferenceKind.DATASET:
        return
    parts = dataset_handle(value).split("/")
    if len(parts) != _HANDLE_PARTS or any(part in {"", ".", ".."} for part in parts):
        msg = f"refusing cache cleanup for invalid dataset handle {value!r}"
        raise ConfigError(msg)
    target = root.joinpath("datasets", *parts)
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        msg = f"refusing cache cleanup outside the dedicated directory: {target}"
        raise ConfigError(msg)
    if target.exists():
        shutil.rmtree(target)


_CLEANERS: dict[ResourceType, Callable[[Path, str], None]] = {
    ResourceType.KAGGLE_DATASET: _kaggle_dataset,
}


def cache_cleanup(root: Path) -> Callable[[str], None]:
    """Validate an explicitly dedicated cache and return its resource cleanup callback."""
    marker = root / CACHE_MARKER
    if (
        not root.is_absolute()
        or root.is_symlink()
        or not marker.is_file()
        or marker.read_text() != CACHE_MARKER_CONTENT
    ):
        msg = f"{root} is not a marked, dedicated profiling cache"
        raise ConfigError(msg)
    if Path(get_cache_folder()).resolve() != root.resolve():
        msg = "--cleanup-cache must match the directory configured by KAGGLEHUB_CACHE"
        raise ConfigError(msg)

    def release(identifier: str) -> None:
        kind, value = parse_resource(identifier)
        cleaner = _CLEANERS.get(kind)
        if cleaner is not None:
            cleaner(root, value)

    return release
