"""Fetch real code from Kaggle kernels.

Searches public kernels by domain keyword, pulls each one's source, and returns its
code cells as raw candidates. Authenticates with a single ``KAGGLE_API_TOKEN`` -- the
opaque ``KGAT_...`` token from the Kaggle settings UI, which the kaggle client reads
natively (it may also be a path to a token file). Needs the optional ``kaggle`` client.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from readie_evals.fetch import FetchError
from readie_evals.models import RawCandidate
from readie_evals.taxonomy import keywords_for


def _authenticated_api() -> Any:
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi  # noqa: PLC0415
    except ImportError as exc:
        msg = "the kaggle client is not installed; `pip install readie-evals[fetch]`"
        raise FetchError(msg) from exc
    # The client resolves KAGGLE_API_TOKEN (the KGAT_ token) itself, so nothing here
    # touches credentials -- it just surfaces a clear error when none are present.
    api = KaggleApi()
    try:
        api.authenticate()
    except Exception as exc:
        msg = f"kaggle authentication failed (set KAGGLE_API_TOKEN): {exc}"
        raise FetchError(msg) from exc
    return api


def _cells_from_pull(directory: Path) -> tuple[str, ...]:
    """Read the code cells out of a pulled kernel directory (a .ipynb or .py).

    A notebook's own cell boundaries are preserved (needed to adapt it into a
    multi-cell session); a plain script has no such boundary and comes back as one.
    """
    for notebook in directory.glob("*.ipynb"):
        raw_cells = json.loads(notebook.read_text()).get("cells", [])
        cells = tuple(
            "".join(cell.get("source", [])).strip()
            for cell in raw_cells
            if cell.get("cell_type") == "code"
        )
        cells = tuple(cell for cell in cells if cell)
        if cells:
            return cells
    for script in directory.glob("*.py"):
        text = script.read_text().strip()
        if text:
            return (text,)
    return ()


class KaggleFetcher:
    """Sources raw candidates from Kaggle kernels."""

    source = "kaggle"

    def __init__(self, api: Any = None, *, per_keyword: int = 5) -> None:
        """Wrap a Kaggle API client, or build one lazily on first fetch.

        Args:
            api: an authenticated ``KaggleApi``; injected in tests. ``None`` builds
                and authenticates one on first use.
            per_keyword: how many kernels to list per search keyword.
        """
        self._api = api
        self._per_keyword = per_keyword

    def _client(self) -> Any:
        if self._api is None:
            self._api = _authenticated_api()
        return self._api

    def fetch(self, *, category: str, count: int) -> list[RawCandidate]:
        """Return up to ``count`` code candidates for ``category``."""
        api = self._client()
        candidates: list[RawCandidate] = []
        for keyword in keywords_for(category):
            if len(candidates) >= count:
                break
            try:
                # No language="python" filter: combined with search, Kaggle's own
                # API reliably returns zero results regardless of the language
                # value (confirmed against the live API -- search alone, or
                # language alone, each work fine; both together silently don't).
                # Non-Python kernels are filtered out downstream instead, by
                # _cells_from_pull finding no .ipynb/.py file to read.
                kernels = api.kernels_list(
                    search=keyword,
                    page_size=self._per_keyword,
                    sort_by="hotness",
                )
            except Exception as exc:
                msg = f"kaggle kernel search failed: {exc}"
                raise FetchError(msg) from exc
            for kernel in kernels:
                if len(candidates) >= count:
                    break
                ref = getattr(kernel, "ref", None)
                if not ref:
                    continue
                candidate = self._pull(api, ref, category)
                if candidate is not None:
                    candidates.append(candidate)
        return candidates

    def _pull(self, api: Any, ref: str, category: str) -> RawCandidate | None:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                api.kernels_pull(ref, path=tmp, metadata=False)
            except Exception:  # noqa: BLE001 - a single unpullable kernel is skipped
                return None
            cells = _cells_from_pull(Path(tmp))
        if not cells:
            return None
        return RawCandidate(
            source=self.source,
            category=category,
            raw_code="\n\n".join(cells),
            raw_cells=cells,
            slug=ref,
            provenance=f"https://www.kaggle.com/code/{ref}",
        )
