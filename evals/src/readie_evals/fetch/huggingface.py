"""Fetch real code from HuggingFace model cards.

Searches the Hub by domain keyword and pulls the Python code blocks out of each
model's card -- the usage snippets practitioners copy. Public models need no token;
set ``HF_TOKEN`` for gated ones. Requires the optional ``huggingface_hub`` client.
"""

from __future__ import annotations

from typing import Any

from readie_evals.fetch import FetchError
from readie_evals.fetch._markdown import python_blocks
from readie_evals.models import RawCandidate
from readie_evals.taxonomy import keywords_for


def _hub_api() -> Any:
    try:
        from huggingface_hub import HfApi  # noqa: PLC0415
    except ImportError as exc:
        msg = "huggingface_hub is not installed; `pip install readie-evals[fetch]`"
        raise FetchError(msg) from exc
    return HfApi()


def _card_text(repo_id: str) -> str | None:
    try:
        from huggingface_hub import ModelCard  # noqa: PLC0415

        return str(ModelCard.load(repo_id).text)
    except Exception:  # noqa: BLE001 - a card that will not load is skipped
        return None


class HuggingFaceFetcher:
    """Sources raw candidates from HuggingFace model cards."""

    source = "huggingface"

    def __init__(self, api: Any = None, *, per_keyword: int = 8) -> None:
        """Wrap a Hub API client, or build one lazily on first fetch.

        Args:
            api: an ``HfApi``; injected in tests. ``None`` builds one on first use.
            per_keyword: how many models to list per search keyword.
        """
        self._api = api
        self._per_keyword = per_keyword

    def _client(self) -> Any:
        if self._api is None:
            self._api = _hub_api()
        return self._api

    def fetch(self, *, category: str, count: int) -> list[RawCandidate]:
        """Return up to ``count`` code candidates for ``category``."""
        api = self._client()
        candidates: list[RawCandidate] = []
        for keyword in keywords_for(category):
            if len(candidates) >= count:
                break
            try:
                # Newer huggingface_hub dropped `direction`; `sort="downloads"` is
                # already highest-first.
                models = api.list_models(
                    search=keyword,
                    limit=self._per_keyword,
                    sort="downloads",
                )
            except Exception as exc:
                msg = f"huggingface model search failed: {exc}"
                raise FetchError(msg) from exc
            for model in models:
                if len(candidates) >= count:
                    break
                repo_id = getattr(model, "id", None) or getattr(model, "modelId", None)
                if not repo_id:
                    continue
                text = _card_text(repo_id)
                blocks = python_blocks(text) if text else []
                if not blocks:
                    continue
                candidates.append(
                    RawCandidate(
                        source=self.source,
                        category=category,
                        raw_code=blocks[0],
                        raw_cells=tuple(blocks),
                        slug=repo_id,
                        provenance=f"https://huggingface.co/{repo_id}",
                    ),
                )
        return candidates
