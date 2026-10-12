"""Real code from the Hugging Face Hub.

``huggingface`` items are Python taken from model, dataset and Space
repositories: fenced blocks in a README, and example, inference, training and
app scripts committed beside it. ``huggingface_notebook`` items are ``.ipynb``
files committed to Hub repositories.

A listing returns each repository's file names with it (``expand=siblings``), so
a request can skip repositories with nothing worth reading before it downloads
anything. Notebooks are rare on the Hub, which is why the scan is wide: a few
hundred repositories per request, and only the ones holding a notebook are read.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from readie_pipeline.corpus.domains import HUB_TERMS, SEARCH_TERMS
from readie_pipeline.corpus.extract import CodeError, parse
from readie_pipeline.corpus.keywords import Keywords
from readie_pipeline.corpus.models import Source
from readie_pipeline.corpus.popularity import Tier, pick_tier
from readie_pipeline.corpus.sources import Candidate
from readie_pipeline.corpus.sources.notebooks import notebook_cells, python_blocks

_FLAVOR_HINTS = {"cpu": ("small", "distil"), "gpu": ("llama", "diffusion", "bert")}
#: Share of requests that append a flavor hint to the search. A hint narrows a
#: name search sharply, so it is applied only now and then.
_HINT_SHARE = 0.3

#: Repositories asked of the Hub for one search. The Hub's search matches names, so
#: a narrow phrase has only a few hundred matches (measured live: "time series"
#: has 235 models whatever the limit), and asking for more than exist is harmless.
_LISTING_LIMIT = 1000
#: Searches one request may walk, each a new (kind, sort) of the phrase, before it
#: reports that it found nothing.
_MAX_QUERIES = 4
#: Repositories read per request, so a request stays bounded however much it finds.
_MAX_REPOS_READ = 30
_MIN_LINES = 3
_MAX_CHARS = 60_000
_MAX_SCRIPTS_PER_REPO = 3

#: Notebooks live in models and Spaces; dataset repositories almost never hold one.
_NOTEBOOK_KINDS = ("model", "space")
_SNIPPET_KINDS = ("model", "dataset", "space")

#: File names that mark a script as a workload rather than packaging or tests.
_SCRIPT_NAME = re.compile(
    r"(^|[_\-/])(app|main|run|train|finetune|fine_tune|inference|infer|predict|eval|"
    r"example|examples|demo|usage|quickstart|tutorial)([_\-.]|$)",
    re.IGNORECASE,
)

#: How far down the ranking to start per tier, and what to rank by. Downloads
#: and likes put the widely used repositories first; recency reaches the tail.
_OFFSETS = {Tier.MAINSTREAM: (0, 25), Tier.COMMON: (25, 200), Tier.NICHE: (200, 600)}
_SORTS = {
    Tier.MAINSTREAM: ("downloads", "likes"),
    Tier.COMMON: ("downloads", "likes"),
    Tier.NICHE: ("last_modified",),
}

Found = list[tuple[str, tuple[str, ...]]]


@dataclass(frozen=True, slots=True)
class Repo:
    """A Hub repository and the file names it holds."""

    kind: str
    id: str
    files: tuple[str, ...]

    @property
    def url(self) -> str:
        """The repository's page."""
        return f"https://huggingface.co/{'' if self.kind == 'model' else self.kind + 's/'}{self.id}"


class HuggingFaceSource:
    """Fetches repository snippets, scripts and notebooks."""

    def __init__(
        self, api: Any, rng: random.Random | None = None, keywords: Keywords | None = None
    ) -> None:
        self._api = api
        self._rng = rng or random.Random()  # noqa: S311 - not security sensitive
        self._keywords = keywords
        self._listings: dict[tuple[str, str, str], list[Repo]] = {}
        self._visited: set[tuple[str, str]] = set()

    def broaden(self, source: Source, domain: str, flavor: str, examples: Sequence[str]) -> int:  # noqa: ARG002
        """Ask the model for more repository-name tokens for this domain."""
        if self._keywords is None:
            return 0
        return self._keywords.broaden("huggingface", domain, flavor, examples)

    def fetch(
        self,
        source: Source,
        domain: str,
        count: int,
        flavor: str,
        known_ref: Callable[[str | None], bool],
    ) -> list[Candidate]:
        """Search the Hub for the domain and read code from what it finds."""
        notebooks = source is Source.HUGGINGFACE_NOTEBOOK
        term = self._term(domain, flavor)
        read = self._notebooks if notebooks else self._snippets

        found: list[Candidate] = []
        repos_read = 0
        for repo in self._repos(term, _NOTEBOOK_KINDS if notebooks else _SNIPPET_KINDS):
            if len(found) >= count or repos_read >= _MAX_REPOS_READ:
                break
            self._visited.add((repo.kind, repo.id))
            if notebooks and not any(f.endswith(".ipynb") for f in repo.files):
                continue  # decided from the listing, with no download
            repos_read += 1
            for ref, code in read(repo):
                if len(found) < count and not known_ref(ref):
                    found.append(Candidate(code, source, domain, ref=ref))
        return found

    def _term(self, domain: str, flavor: str) -> str:
        """A Hub search phrase for the domain, sometimes steered toward the flavor."""
        if self._keywords is None:
            term = self._rng.choice((*SEARCH_TERMS[domain], *HUB_TERMS[domain]))
        else:
            term = self._keywords.pick("huggingface", domain, flavor, self._rng)
        if self._rng.random() < _HINT_SHARE:
            term = f"{term} {self._rng.choice(_FLAVOR_HINTS[flavor])}"
        return term

    def _repos(self, term: str, kinds: tuple[str, ...]) -> Iterable[Repo]:
        """Repositories matching ``term`` that no request has looked at yet.

        Each search is listed once and kept, and a request starts at a point in it
        chosen by the tier, wrapping round, so repeated requests walk on through
        the same search instead of re-reading its head. Once a search has nothing
        unseen the next one tried is another kind or sort of the same phrase.
        """
        for _ in range(_MAX_QUERIES):
            kind = self._rng.choice(kinds)
            tier = pick_tier(self._rng)
            sort = self._rng.choice(_SORTS[tier])
            if kind == "space" and sort == "downloads":
                sort = "likes"  # Spaces have no download count
            repos = self._listing(kind, term, sort)
            if not repos:
                continue
            start = self._rng.randint(*_OFFSETS[tier])
            if start >= len(repos):
                start = self._rng.randint(0, len(repos) - 1)
            for step in range(len(repos)):
                repo = repos[(start + step) % len(repos)]
                if (repo.kind, repo.id) not in self._visited:
                    yield repo

    def _listing(self, kind: str, term: str, sort: str) -> list[Repo]:
        key = (kind, term, sort)
        if key not in self._listings:
            listing = {
                "model": self._api.list_models,
                "dataset": self._api.list_datasets,
                "space": self._api.list_spaces,
            }[kind]
            infos = listing(search=term, sort=sort, limit=_LISTING_LIMIT, expand=["siblings"])
            self._listings[key] = [
                Repo(
                    kind,
                    info.id,
                    tuple(s.rfilename for s in (getattr(info, "siblings", None) or ())),
                )
                for info in infos
            ]
        return self._listings[key]

    def _snippets(self, repo: Repo) -> Found:
        """Python from a repository: example scripts first, then README blocks."""
        found: Found = []
        if repo.kind != "dataset":
            # A dataset repository's scripts are loaders, not workloads.
            scripts = [f for f in repo.files if f.endswith(".py") and _SCRIPT_NAME.search(f)]
            for name in scripts[:_MAX_SCRIPTS_PER_REPO]:
                text = self._read(repo, name)
                if text and _usable(text):
                    found.append((f"{repo.url}/blob/main/{name}", (text,)))

        if "README.md" in repo.files:
            card = self._read(repo, "README.md")
            blocks = [b for b in python_blocks(card or "") if _usable(b)]
            found.extend((f"{repo.url}#snippet-{i}", (block,)) for i, block in enumerate(blocks))
        return found

    def _notebooks(self, repo: Repo) -> Found:
        found: Found = []
        for name in (f for f in repo.files if f.endswith(".ipynb")):
            cells = notebook_cells(self._read(repo, name) or "")
            if cells:
                found.append((f"{repo.url}/blob/main/{name}", cells))
        return found

    def _read(self, repo: Repo, filename: str) -> str | None:
        from huggingface_hub import hf_hub_download  # noqa: PLC0415 - optional extra

        try:
            path = hf_hub_download(
                repo.id, filename, repo_type=repo.kind, token=self._api.token or None
            )
            with open(path, encoding="utf-8") as handle:  # noqa: PTH123
                return handle.read()
        except Exception:  # noqa: BLE001 - missing or unreadable files are skipped
            return None


def _parses(code: str) -> bool:
    try:
        parse(code)
    except CodeError:
        return False
    return True


def _usable(text: str) -> bool:
    return len(text.splitlines()) >= _MIN_LINES and len(text) <= _MAX_CHARS and _parses(text)
