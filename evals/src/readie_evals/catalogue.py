"""What the CPU checkpoints already carry.

The router restores a checkpoint that has already imported a set of packages; a
workload whose imports are in that set starts warm. This module reads the deployed
``catalogues/cpu.json`` to learn which import names are pre-imported, so the
adapter can prefer them and the runner can tell which imports still need a
per-call ``packages=`` install.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: Where the deployed CPU catalogue lives, relative to the repo root. Two parents
#: up from this file is ``evals/``; three is the repo root.
_DEFAULT_CATALOGUE = Path(__file__).resolve().parents[3] / "catalogues" / "cpu.json"

#: Import name -> PyPI distribution, for the common cases where they differ. Used
#: to turn a non-warm import into a ``packages=`` requirement string.
IMPORT_TO_DISTRIBUTION: dict[str, str] = {
    "sklearn": "scikit-learn",
    "PIL": "pillow",
    "cv2": "opencv-python-headless",
    "skimage": "scikit-image",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "sentence_transformers": "sentence-transformers",
    "dateutil": "python-dateutil",
}


class CatalogueError(Exception):
    """The catalogue on disk is not what this code expects."""


def default_catalogue_path() -> Path:
    """The deployed CPU catalogue path."""
    return _DEFAULT_CATALOGUE


def warm_imports(path: Path | None = None) -> frozenset[str]:
    """Return every import name pre-imported by some CPU checkpoint.

    The union of every checkpoint's ``canonical`` list: the packages a request
    can rely on being resident. A missing catalogue yields an empty set (every
    call then cold-starts, which is still measurable) rather than an error.
    """
    catalogue = path or _DEFAULT_CATALOGUE
    if not catalogue.exists():
        return frozenset()
    try:
        raw = json.loads(catalogue.read_text())
    except json.JSONDecodeError as exc:  # pragma: no cover - a corrupt catalogue
        msg = f"catalogue at {catalogue} is not valid JSON: {exc}"
        raise CatalogueError(msg) from exc

    checkpoints = raw.get("checkpoints", []) if isinstance(raw, dict) else []
    names: set[str] = set()
    for checkpoint in checkpoints:
        names.update(checkpoint.get("canonical", ()))
    return frozenset(names)


def distribution_for(import_name: str) -> str:
    """The PyPI requirement to install for a top-level import name."""
    return IMPORT_TO_DISTRIBUTION.get(import_name, import_name)


def missing_packages(imports: tuple[str, ...], warm: frozenset[str]) -> tuple[str, ...]:
    """Packages an import list needs beyond the checkpoint and the standard library.

    A warm import needs nothing; a standard-library import needs nothing; anything
    else becomes a ``packages=`` requirement so the executor installs it before
    the call.
    """
    stdlib = sys.stdlib_module_names
    extras = {distribution_for(name) for name in imports if name not in warm and name not in stdlib}
    return tuple(sorted(extras))
