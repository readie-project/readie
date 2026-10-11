"""Real code and existence checks from Kaggle.

``kaggle`` source items are Python script kernels; ``kaggle_notebook`` items
are notebook kernels. Both are fetched through the Kaggle API: nothing here is
generated.
"""

from __future__ import annotations

import json
import os
import random
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from readie_pipeline.corpus.domains import SEARCH_TERMS
from readie_pipeline.corpus.keywords import Keywords
from readie_pipeline.corpus.models import CorpusError, ResourceKind, Source, resource
from readie_pipeline.corpus.popularity import Tier, pick_tier
from readie_pipeline.corpus.sources import Candidate
from readie_pipeline.corpus.sources.notebooks import notebook_cells
from readie_pipeline.corpus.sources.pacing import RETRY_STATUSES, Pacer, http_status
from readie_pipeline.corpus.verify import ResourceLookupError

_FLAVOR_HINTS = {"cpu": ("scikit-learn", "pandas", "xgboost"), "gpu": ("pytorch", "gpu", "cuda")}
#: Share of requests that append a flavor hint to the search. Kaggle search needs
#: every word to match, so a hint narrows it sharply: measured live, "seasonal
#: decomposition scikit-learn" returned 0 script kernels and "seasonal
#: decomposition" returned 30. A hint is therefore the exception, and a hinted
#: search that finds nothing is repeated without it.
_HINT_SHARE = 0.2
#: Where to look per tier: result pages, and how to rank. The most-voted kernels
#: are the ones most people have read and copied; deep pages ranked by recency
#: reach the long tail.
_PAGES = {
    Tier.MAINSTREAM: (1, 3),
    Tier.COMMON: (4, 10),
    Tier.NICHE: (11, 30),
}
_SORTS = {
    Tier.MAINSTREAM: ("voteCount", "viewCount"),
    Tier.COMMON: ("hotness", "voteCount"),
    Tier.NICHE: ("dateRun", "relevance"),
}
#: Kaggle's ``language`` filter matches nothing once a search is given, so it is
#: not sent. ``script`` returns Python and R scripts and ``all`` returns
#: notebooks; the pulled file's extension then removes the R ones.
_KERNEL_TYPE = {Source.KAGGLE: "script", Source.KAGGLE_NOTEBOOK: "all"}
_ALL_SORTS = tuple(dict.fromkeys(s for sorts in _SORTS.values() for s in sorts))
_MAX_PULLS_PER_CALL = 40
_PAGE_SIZE = 30
#: Kaggle stops answering past about page 35 of a search (measured live: pages up
#: to 34 are full, 35 and later are empty), so a search holds roughly a thousand
#: kernels however many match. Everything past that is reached with other phrases,
#: sorts and kernel types, which is why a query is walked page by page and left
#: behind once it is used up.
_MAX_PAGE = 40
#: Result pages one request may read before it reports that it found nothing.
_MAX_LISTINGS = 6
#: A search that ends before this page holds all of its results: sorting only
#: reorders them, so reading it again under another sort finds the same kernels.
#: One that reaches this page was cut off by Kaggle, and each sort reaches a
#: different part of it.
_TRUNCATED_AT = 34
#: Picks of a phrase made to find one that has not been used up.
_PICKS_PER_TERM = 5
#: Downloads run together in groups this big. The pacer still spaces the requests.
_PULL_WORKERS = 6
_KERNEL_URL = "https://www.kaggle.com/code/"
_METADATA_FILE = "kernel-metadata.json"


def make_api(token: str) -> Any:
    """An authenticated Kaggle client.

    The ``kaggle`` package authenticates when it is imported, so the import
    happens here, after the credential is in the environment, and is skipped
    entirely when no Kaggle source is in use. ``token`` is the access token
    from the Kaggle settings page.
    """
    os.environ.setdefault("KAGGLE_API_TOKEN", token)
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi  # noqa: PLC0415 - optional extra
    except (ImportError, SystemExit) as exc:
        msg = "could not authenticate with Kaggle; check KAGGLE_API_TOKEN"
        raise CorpusError(msg) from exc
    api = KaggleApi()
    api.authenticate()
    return api


class _Walk:
    """Which pages of one search were read, and where its results end."""

    __slots__ = ("last", "visited")

    def __init__(self) -> None:
        self.visited: set[int] = set()
        self.last = _MAX_PAGE

    def next_page(self, start: int) -> int | None:
        """The first unread page from ``start`` on, wrapping round; ``None`` once all are read."""
        for step in range(_MAX_PAGE):
            page = (start - 1 + step) % _MAX_PAGE + 1
            if page <= self.last and page not in self.visited:
                return page
        return None

    def read(self, page: int, count: int) -> int | None:
        """Record a page; when it came back short, the last page of the results."""
        self.visited.add(page)
        if count == 0:
            self.last = min(self.last, page - 1)
            return page - 1
        if count < _PAGE_SIZE:
            self.last = min(self.last, page)
            return page
        return None


class KaggleSource:
    """Fetches script and notebook kernels."""

    def __init__(
        self,
        api: Any,
        rng: random.Random | None = None,
        keywords: Keywords | None = None,
        pacer: Pacer | None = None,
    ) -> None:
        self._api = api
        self._rng = rng or random.Random()  # noqa: S311 - not security sensitive
        self._keywords = keywords
        self._pacer = pacer or Pacer()
        self._walks: dict[tuple[str, str, str], _Walk] = {}
        #: Searches whose whole result set was read, and the sort that read it.
        self._whole: dict[tuple[str, str], str] = {}
        #: The phrase each (source, domain) is working through, and the phrases used up.
        self._current: dict[tuple[Source, str, str], tuple[str, ...]] = {}
        self._spent: set[str] = set()
        self._pulled: set[str] = set()

    def broaden(self, source: Source, domain: str, flavor: str, examples: Sequence[str]) -> int:  # noqa: ARG002
        """Ask the model for more search phrases for this domain."""
        if self._keywords is None:
            return 0
        return self._keywords.broaden("kaggle", domain, flavor, examples)

    def _term(self, domain: str, flavor: str) -> str:
        if self._keywords is None:
            return self._rng.choice(SEARCH_TERMS[domain])
        return self._keywords.pick("kaggle", domain, flavor, self._rng)

    def fetch(
        self,
        source: Source,
        domain: str,
        count: int,
        flavor: str,
        known_ref: Callable[[str | None], bool],
    ) -> list[Candidate]:
        """Search kernels for the domain and pull the ones not yet seen.

        A phrase is worked through before another is picked: its result pages are
        read in turn, under each sort that can still reach new kernels, and it is
        left behind only once all of them are read. A request reads up to its page
        budget, and a page of kernels already seen does not end it.
        """
        found: list[Candidate] = []
        key = (source, domain, flavor)
        for _ in range(_MAX_LISTINGS):
            queries = self._current.get(key) or self._current.setdefault(
                key, self._start_phrase(domain, flavor)
            )
            listing = self._next_listing(source, queries)
            if listing is None:
                self._spent.add(queries[-1])  # the plain phrase is always the last query
                del self._current[key]
                continue
            fresh = [
                meta
                for meta in listing[:_MAX_PULLS_PER_CALL]
                if meta is not None
                and meta.ref not in self._pulled
                and not known_ref(f"{_KERNEL_URL}{meta.ref}")
            ]
            found.extend(self._pull_all(fresh, source, domain, count - len(found)))
            if len(found) >= count:
                break
        return found

    def _start_phrase(self, domain: str, flavor: str) -> tuple[str, ...]:
        """The searches for a phrase not used up: sometimes with a flavor hint, then plain."""
        term = self._term(domain, flavor)
        for _ in range(_PICKS_PER_TERM):
            if term not in self._spent:
                break
            term = self._term(domain, flavor)
        if self._rng.random() < _HINT_SHARE:
            return (f"{term} {self._rng.choice(_FLAVOR_HINTS[flavor])}", term)
        return (term,)

    def _next_listing(self, source: Source, queries: tuple[str, ...]) -> list[Any] | None:
        """The next unread page of the first search that has one; ``None`` when all are used up."""
        kernel_type = _KERNEL_TYPE[source]
        tier = pick_tier(self._rng)
        preferred = self._rng.choice(_SORTS[tier])
        start = self._rng.randint(*_PAGES[tier])
        for query in queries:
            whole = self._whole.get((query, kernel_type))
            sorts = [whole] if whole else [preferred, *(s for s in _ALL_SORTS if s != preferred)]
            for sort_by in sorts:
                walk = self._walks.setdefault((query, kernel_type, sort_by), _Walk())
                page = walk.next_page(start)
                if page is None:
                    continue
                listing = self._list(query, source, page, sort_by)
                end = walk.read(page, len(listing))
                if end is not None and end < _TRUNCATED_AT:
                    self._whole[(query, kernel_type)] = sort_by
                if listing:
                    return listing
                break  # an empty page ends this search's results under every sort
        return None

    def _pull_all(
        self, kernels: list[Any], source: Source, domain: str, count: int
    ) -> list[Candidate]:
        """Download kernels a group at a time, stopping once ``count`` are usable.

        Each download is a request that takes seconds, so doing them one by one
        made a pair needing a hundred items take most of an hour. A group runs
        together; the shared pacer still spaces the requests, so the rate to Kaggle
        is the same and only the waiting overlaps. A group may return a few more
        than ``count``, and all of them are kept.
        """
        found: list[Candidate] = []
        with ThreadPoolExecutor(max_workers=_PULL_WORKERS) as pool:
            for start in range(0, len(kernels), _PULL_WORKERS):
                group = kernels[start : start + _PULL_WORKERS]
                pulled = list(pool.map(lambda meta: self._pull(meta.ref, source), group))
                self._pulled.update(meta.ref for meta in group)
                for meta, (code, declared) in zip(group, pulled, strict=True):
                    if code:
                        ref = f"{_KERNEL_URL}{meta.ref}"
                        found.append(Candidate(code, source, domain, ref=ref, declared=declared))
                if len(found) >= count:
                    break
        return found

    def _list(self, search: str, source: Source, page: int, sort_by: str) -> list[Any]:
        return (
            self._pacer.call(
                lambda: self._api.kernels_list(
                    page=page,
                    page_size=_PAGE_SIZE,
                    search=search,
                    kernel_type=_KERNEL_TYPE[source],
                    sort_by=sort_by,
                )
            )
            or []
        )

    def _pull(self, kernel: str, source: Source) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The kernel's code and attached resources, if it is the shape ``source`` wants.

        The kind is read from what was pulled: ``.py`` is a Python script,
        ``.ipynb`` a Python notebook, and R and other kernels pull as something
        else and are skipped. The datasets and models a kernel is attached to are
        in its ``kernel-metadata.json``, not in the search listing.
        """
        suffix = ".ipynb" if source is Source.KAGGLE_NOTEBOOK else ".py"
        with tempfile.TemporaryDirectory() as scratch:
            try:
                self._pacer.call(
                    lambda: self._api.kernels_pull(kernel, scratch, metadata=True, quiet=True)
                )
            except Exception as exc:
                # A private, deleted or unreadable kernel is skipped. A kernel the
                # service still refuses after the pacer has waited is not: that is
                # throttling, not an unusable kernel, and it must surface so the
                # run reports it instead of counting the kernel as empty.
                if http_status(exc) in RETRY_STATUSES:
                    raise
                return (), ()
            files = [p for p in Path(scratch).iterdir() if p.is_file() and p.suffix == suffix]
            metadata = _read_metadata(Path(scratch) / _METADATA_FILE)
            if not files or metadata.get("language", "python") != "python":
                return (), ()
            text = files[0].read_text(encoding="utf-8", errors="replace")
            code = notebook_cells(text) if suffix == ".ipynb" else (text,)
            return code, _declared(metadata)


def _read_metadata(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _slugs(values: object, parts: int) -> list[str]:
    """Entries that are real ``owner/name`` slugs; metadata can hold blanks."""
    if not isinstance(values, list):
        return []
    slugs = [v.strip() for v in values if isinstance(v, str)]
    return [v for v in slugs if len([p for p in v.split("/") if p]) >= parts]


def _declared(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    """Datasets and models a kernel is attached to, from its metadata.

    ``dataset_sources`` hold ``owner/slug``. ``model_sources`` hold
    ``owner/model/framework/variation/version``, of which ``owner/model`` is the
    part that can be looked up. Competition data has no resource kind yet.
    """
    declared = [
        resource(ResourceKind.KAGGLE_DATASET, slug)
        for slug in _slugs(metadata.get("dataset_sources"), parts=2)
    ]
    declared.extend(
        resource(ResourceKind.KAGGLE_MODEL, "/".join(slug.split("/")[:2]))
        for slug in _slugs(metadata.get("model_sources"), parts=2)
    )
    return tuple(declared)


#: Kaggle answers 403 for a dataset or model that does not exist or is not
#: public, and 404 for some missing paths. Either means there is no public
#: resource of that name, which is what a corpus can use.
_ABSENT_STATUSES = frozenset({403, 404})


class KaggleLookup:
    """Existence checks against Kaggle, as :class:`~verify.KaggleCatalog`.

    ``dataset_status`` is not used: it answers 404 for datasets the caller does
    not own, so it reports every real public dataset as missing. The checks share
    the source's :class:`Pacer`, so both stay inside one request budget.
    """

    def __init__(self, api: Any, pacer: Pacer | None = None) -> None:
        self._api = api
        self._pacer = pacer or Pacer()

    def dataset_exists(self, slug: str) -> bool:
        """Whether ``owner/slug`` is a public Kaggle dataset."""
        return self._probe(lambda: self._api.dataset_list_files(slug))

    def model_exists(self, slug: str) -> bool:
        """Whether ``owner/model`` is a public Kaggle model."""
        return self._probe(lambda: self._api.model_get(slug))

    def _probe(self, call: Callable[[], object]) -> bool:
        try:
            self._pacer.call(call)
        except Exception as exc:
            if http_status(exc) in _ABSENT_STATUSES:
                return False
            raise ResourceLookupError(str(exc)) from exc
        return True
