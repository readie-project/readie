import contextlib
import json
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from readie_pipeline.corpus.models import Source
from readie_pipeline.corpus.sources.huggingface import HuggingFaceSource
from readie_pipeline.corpus.sources.kaggle import KaggleLookup, KaggleSource
from readie_pipeline.corpus.sources.pacing import Pacer
from readie_pipeline.corpus.verify import ResourceLookupError


class First(random.Random):
    """Always takes the first option, so a source's choices are predictable."""

    def choice(self, seq):
        return seq[0]

    def choices(self, population, weights=None, **_):
        return [population[0]]

    def randint(self, a, b):
        return a

    def random(self):
        return 0.99  # never below a probability threshold


@pytest.fixture(autouse=True)
def no_real_pauses(monkeypatch):
    """Sources pause between downloads and when rate limited; tests must not wait."""
    monkeypatch.setattr("readie_pipeline.corpus.sources.pacing.time.sleep", lambda _: None)


def never(_ref):
    return False


# ---------------------------------------------------------------------------
# Kaggle
# ---------------------------------------------------------------------------
class FakeKaggle:
    def __init__(self, kernels, files, metadata=None):
        self.kernels = kernels
        self.files = files
        self.metadata = metadata or {}
        self.listed: list[dict[str, object]] = []
        self.pulled: list[str] = []

    def kernels_list(self, **kwargs):
        self.listed.append(kwargs)
        return self.kernels

    def kernels_pull(self, kernel, path, metadata=False, quiet=True):
        self.pulled.append(kernel)
        name, text = self.files[kernel]
        (Path(path) / name).write_text(text)
        if metadata:
            meta = {"language": "python", **self.metadata.get(kernel, {})}
            (Path(path) / "kernel-metadata.json").write_text(json.dumps(meta))


def kernel(ref):
    return SimpleNamespace(ref=ref)


def test_a_script_kernel_becomes_one_string_with_its_attached_datasets_declared():
    # Pinned: the search listing carries no attached datasets (its fields come back
    # empty), so they are read from the pulled kernel-metadata.json.
    api = FakeKaggle(
        [kernel("o/eda")],
        {"o/eda": ("eda.py", "import pandas as pd\n")},
        {
            "o/eda": {
                "dataset_sources": ["o/titanic"],
                "model_sources": ["google/gemma/pyTorch/2b/1"],
                "competition_sources": ["titanic"],
            }
        },
    )
    found = KaggleSource(api, First()).fetch(
        Source.KAGGLE, "Exploratory Data Analysis", 3, "cpu", never
    )

    # Pinned: Kaggle's `language` filter returns nothing once a search is given,
    # so it must not be sent. Scripts need `script`, notebooks need `all`.
    assert "language" not in api.listed[0]
    assert api.listed[0]["kernel_type"] == "script"
    assert found[0].code == ("import pandas as pd\n",)
    assert found[0].ref == "https://www.kaggle.com/code/o/eda"
    assert found[0].declared == ("kaggle_dataset:o/titanic", "kaggle_model:google/gemma")


def test_a_notebook_kernel_becomes_its_code_cells():
    nb = {
        "cells": [
            {"cell_type": "code", "source": ["a = 1"]},
            {"cell_type": "markdown", "source": ["x"]},
        ]
    }
    api = FakeKaggle([kernel("o/nb")], {"o/nb": ("nb.ipynb", json.dumps(nb))})
    found = KaggleSource(api, First()).fetch(
        Source.KAGGLE_NOTEBOOK, "Data Science", 3, "cpu", never
    )

    assert found[0].code == ("a = 1",)
    assert api.listed[0]["kernel_type"] == "all"


class AlwaysHint(First):
    """Applies the flavor hint on every request."""

    def random(self):
        return 0.0


class Searches(FakeKaggle):
    """Returns kernels only for queries that do not contain the hint word."""

    def kernels_list(self, **kwargs):
        self.listed.append(kwargs)
        return [] if "scikit-learn" in kwargs["search"] else self.kernels


def test_a_hinted_search_that_finds_nothing_is_repeated_without_the_hint():
    # Pinned: Kaggle needs every word to match, so "seasonal decomposition
    # scikit-learn" found 0 script kernels where "seasonal decomposition" found 30.
    api = Searches([kernel("o/k")], {"o/k": ("k.py", "x = 1")})
    found = KaggleSource(api, AlwaysHint()).fetch(Source.KAGGLE, "Data Science", 1, "cpu", never)

    assert [call["search"] for call in api.listed] == ["data science scikit-learn", "data science"]
    assert len(found) == 1


def test_the_flavor_hint_is_the_exception_not_the_rule():
    api = FakeKaggle([], {})
    KaggleSource(api, First()).fetch(Source.KAGGLE, "Data Science", 1, "cpu", never)
    assert api.listed[0]["search"] == "data science"


def test_a_search_without_a_hint_is_not_repeated_when_it_finds_nothing():
    api = FakeKaggle([], {})
    KaggleSource(api, First()).fetch(Source.KAGGLE, "Data Science", 1, "cpu", never)
    assert len(api.listed) == 1


class RateLimited(FakeKaggle):
    """Answers ``status`` to the first ``failures`` calls of each kind, then works."""

    def __init__(self, failures, kernels, files, status=429):
        super().__init__(kernels, files)
        self.failures = {"list": failures, "pull": failures}
        self.status = status

    def kernels_list(self, **kwargs):
        if self.failures["list"] > 0:
            self.failures["list"] -= 1
            raise HttpError(self.status, "Too Many Requests")
        return super().kernels_list(**kwargs)

    def kernels_pull(self, kernel, path, metadata=False, quiet=True):
        if self.failures["pull"] > 0:
            self.failures["pull"] -= 1
            raise HttpError(self.status, "Too Many Requests")
        super().kernels_pull(kernel, path, metadata, quiet)


class PullRefused(FakeKaggle):
    """Lists fine, but refuses every download with ``status``."""

    def __init__(self, status, kernels):
        super().__init__(kernels, {})
        self.status = status

    def kernels_pull(self, kernel, path, metadata=False, quiet=True):
        raise HttpError(self.status, "refused")


def pacer(**overrides):
    """A pacer that records its waits instead of sleeping, with no spacing between calls."""
    delays: list[float] = []
    return delays, Pacer(base_interval=0.0, sleep=delays.append, **overrides)


def test_a_rate_limited_search_is_retried_with_growing_pauses_and_then_succeeds():
    # Seen live: 429s ended a pair after three failed searches.
    delays, p = pacer()
    api = RateLimited(3, [kernel("o/k")], {"o/k": ("k.py", "x = 1")})
    found = KaggleSource(api, First(), pacer=p).fetch(
        Source.KAGGLE, "Data Science", 1, "cpu", never
    )

    assert len(found) == 1
    # Doubling for the search, then again for the download.
    assert delays == [5.0, 10.0, 20.0, 5.0, 10.0, 20.0]


def test_a_rate_limited_download_is_retried_not_silently_dropped():
    # Before: every pull error was swallowed, so a 429 looked like an unusable kernel.
    _, p = pacer()
    api = RateLimited(2, [kernel("o/k")], {"o/k": ("k.py", "x = 1")})
    found = KaggleSource(api, First(), pacer=p).fetch(
        Source.KAGGLE, "Data Science", 1, "cpu", never
    )
    assert [c.code for c in found] == [("x = 1",)]


def test_a_download_that_stays_rate_limited_surfaces_instead_of_reading_as_empty():
    # Seen live: a pair sat idle for 40 minutes under sustained 429s because every
    # refused download came back as "no usable code" and nothing was logged.
    _, p = pacer(attempts=3)
    with pytest.raises(HttpError):
        KaggleSource(PullRefused(429, [kernel("o/k")]), First(), pacer=p).fetch(
            Source.KAGGLE, "Data Science", 1, "cpu", never
        )


@pytest.mark.parametrize("status", [403, 404])
def test_a_private_or_deleted_kernel_is_skipped_without_waiting(status):
    delays, p = pacer()
    api = PullRefused(status, [kernel("o/gone")])
    found = KaggleSource(api, First(), pacer=p).fetch(
        Source.KAGGLE, "Data Science", 1, "cpu", never
    )
    assert found == []
    assert delays == []


def test_a_search_that_stays_rate_limited_gives_up_after_a_bounded_number_of_tries():
    delays, p = pacer(attempts=4)
    api = RateLimited(99, [kernel("o/k")], {"o/k": ("k.py", "x = 1")})
    with pytest.raises(HttpError):
        KaggleSource(api, First(), pacer=p).fetch(Source.KAGGLE, "Data Science", 1, "cpu", never)
    assert len(delays) == 3


def test_a_status_that_will_not_improve_is_not_retried():
    delays, p = pacer()
    api = RateLimited(99, [kernel("o/k")], {"o/k": ("k.py", "x = 1")}, status=403)
    with pytest.raises(HttpError):
        KaggleSource(api, First(), pacer=p).fetch(Source.KAGGLE, "Data Science", 1, "cpu", never)
    assert delays == []


def test_existence_checks_share_the_sources_pacer():
    delays, p = pacer(attempts=3)

    class Throttled:
        def dataset_list_files(self, slug):
            raise HttpError(429, "slow down")

    with pytest.raises(ResourceLookupError):
        KaggleLookup(Throttled(), p).dataset_exists("o/d")
    assert delays == [5.0, 10.0]


def test_blank_and_malformed_metadata_entries_are_not_declared():
    # Seen live: a kernel's dataset_sources held an empty string, which became
    # the resource "kaggle_dataset:" and aborted the whole run.
    api = FakeKaggle(
        [kernel("o/k")],
        {"o/k": ("k.py", "x = 1")},
        {"o/k": {"dataset_sources": ["", "  ", "no-owner", "o/ok", 7], "model_sources": ["", "m"]}},
    )
    found = KaggleSource(api, First()).fetch(Source.KAGGLE, "Data Science", 1, "cpu", never)
    assert found[0].declared == ("kaggle_dataset:o/ok",)


def test_a_kernel_whose_metadata_says_it_is_not_python_is_skipped():
    api = FakeKaggle([kernel("o/r")], {"o/r": ("r.py", "x = 1")}, {"o/r": {"language": "r"}})
    assert KaggleSource(api, First()).fetch(Source.KAGGLE, "Data Science", 2, "cpu", never) == []


def test_a_notebook_is_not_taken_as_a_script_and_a_script_is_not_taken_as_a_notebook():
    nb = json.dumps({"cells": [{"cell_type": "code", "source": ["a = 1"]}]})
    files = {"o/nb": ("nb.ipynb", nb), "o/py": ("s.py", "x = 1"), "o/r": ("r.R", "library(x)")}
    kernels = [kernel("o/nb"), kernel("o/py"), kernel("o/r")]

    scripts = KaggleSource(FakeKaggle(kernels, files), First()).fetch(
        Source.KAGGLE, "Data Science", 5, "cpu", never
    )
    notebooks = KaggleSource(FakeKaggle(kernels, files), First()).fetch(
        Source.KAGGLE_NOTEBOOK, "Data Science", 5, "cpu", never
    )

    assert [c.code for c in scripts] == [("x = 1",)]
    assert [c.code for c in notebooks] == [("a = 1",)]


def test_a_kernel_that_cannot_be_pulled_is_skipped_not_fatal():
    class Flaky(FakeKaggle):
        def kernels_pull(self, kernel, path, metadata=False, quiet=True):
            if kernel == "o/gone":
                raise RuntimeError("404 Not Found")
            super().kernels_pull(kernel, path, metadata, quiet)

    api = Flaky([kernel("o/gone"), kernel("o/ok")], {"o/ok": ("ok.py", "x = 1")})
    found = KaggleSource(api, First()).fetch(Source.KAGGLE, "Data Science", 2, "cpu", never)
    assert [c.ref for c in found] == ["https://www.kaggle.com/code/o/ok"]


def test_kernels_already_seen_are_not_pulled_again():
    api = FakeKaggle([kernel("o/a"), kernel("o/b")], {"o/b": ("b.py", "x = 1")})
    found = KaggleSource(api, First()).fetch(
        Source.KAGGLE, "Data Science", 5, "cpu", lambda ref: ref is not None and ref.endswith("o/a")
    )
    assert api.pulled == ["o/b"]
    assert len(found) == 1


def test_downloads_stop_after_the_group_that_supplies_enough_and_nothing_is_wasted_beyond_it():
    from readie_pipeline.corpus.sources.kaggle import _PULL_WORKERS

    kernels = [kernel(f"o/k{i}") for i in range(30)]
    api = FakeKaggle(kernels, {k.ref: ("k.py", "x = 1") for k in kernels})
    found = KaggleSource(api, First()).fetch(Source.KAGGLE, "Data Science", 2, "cpu", never)

    # One group of downloads runs together; all of what it returns is kept.
    assert len(api.pulled) == _PULL_WORKERS
    assert len(found) == _PULL_WORKERS


def test_downloads_run_together_so_their_waiting_overlaps():
    # Seen live: each download takes seconds, and doing them one at a time made a
    # pair needing a hundred items take most of an hour.
    import threading

    in_flight = 0
    peak = 0
    lock = threading.Lock()
    gate = threading.Barrier(3, timeout=5)

    class Slow(FakeKaggle):
        def kernels_pull(self, kernel, path, metadata=False, quiet=True):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            with contextlib.suppress(threading.BrokenBarrierError):
                gate.wait()  # only returns once three downloads are in progress together
            super().kernels_pull(kernel, path, metadata, quiet)
            with lock:
                in_flight -= 1

    kernels = [kernel(f"o/k{i}") for i in range(3)]
    api = Slow(kernels, {k.ref: ("k.py", "x = 1") for k in kernels})
    found = KaggleSource(api, First()).fetch(Source.KAGGLE, "Data Science", 3, "cpu", never)

    assert len(found) == 3
    assert peak == 3


def test_a_download_that_fails_in_a_group_does_not_lose_the_others():
    class OneGone(FakeKaggle):
        def kernels_pull(self, kernel, path, metadata=False, quiet=True):
            if kernel == "o/k1":
                raise HttpError(404, "gone")
            super().kernels_pull(kernel, path, metadata, quiet)

    kernels = [kernel(f"o/k{i}") for i in range(3)]
    api = OneGone(kernels, {k.ref: ("k.py", f"x = {k.ref[-1]}") for k in kernels})
    found = KaggleSource(api, First()).fetch(Source.KAGGLE, "Data Science", 3, "cpu", never)
    assert [str(c.ref)[-2:] for c in found] == ["k0", "k2"]


class Paged(FakeKaggle):
    """A search with ``total`` kernels, served 30 to a page like Kaggle, and none past the end."""

    def __init__(self, total):
        refs = [f"o/k{i}" for i in range(total)]
        super().__init__([], dict.fromkeys(refs, ("k.py", "x = 1")))
        self.refs = refs

    def kernels_list(self, **kwargs):
        self.listed.append(kwargs)
        start = (kwargs["page"] - 1) * 30
        return [kernel(r) for r in self.refs[start : start + 30]]


def test_pages_already_read_are_not_read_again_and_the_search_walks_on():
    # Pinned: a request used to draw one random page and give up on a pair after
    # three pages of kernels it had already seen; a search holds about a thousand
    # kernels and was never walked.
    api = Paged(90)
    source = KaggleSource(api, First())
    for _ in range(3):
        source.fetch(Source.KAGGLE, "Data Science", 30, "cpu", never)

    assert [c["page"] for c in api.listed] == [1, 2, 3]
    assert len(set(api.pulled)) == 90


def test_a_request_moves_past_a_page_of_seen_kernels_instead_of_returning_empty():
    api = Paged(60)
    seen = {f"https://www.kaggle.com/code/o/k{i}" for i in range(30)}
    found = KaggleSource(api, First()).fetch(
        Source.KAGGLE, "Data Science", 3, "cpu", lambda ref: ref in seen
    )

    assert [c["page"] for c in api.listed] == [1, 2]
    assert len(found) >= 3


def test_a_search_that_has_run_out_of_pages_is_left_alone():
    api = Paged(45)
    source = KaggleSource(api, First())
    for _ in range(4):
        source.fetch(Source.KAGGLE, "Data Science", 100, "cpu", never)

    # Page 2 came back short, so there is no page 3 to ask for.
    assert [c["page"] for c in api.listed] == [1, 2]


class Terms:
    """A phrase bank that hands out its phrases in order."""

    def __init__(self, *terms):
        self.terms = list(terms)
        self.picks = 0

    def pick(self, platform, domain, flavor, rng):
        term = self.terms[min(self.picks, len(self.terms) - 1)]
        self.picks += 1
        return term

    def broaden(self, platform, domain, flavor, examples=()):
        return 0


class ByTerm(FakeKaggle):
    """Each phrase has its own number of kernels, 30 to a page."""

    def __init__(self, totals):
        super().__init__([], {})
        self.totals = totals

    def kernels_list(self, **kwargs):
        self.listed.append(kwargs)
        total = self.totals.get(kwargs["search"], 0)
        start = (kwargs["page"] - 1) * 30
        refs = [f"o/{kwargs['search']}-{i}" for i in range(total)][start : start + 30]
        for ref in refs:
            self.files[ref] = ("k.py", "x = 1")
        return [kernel(r) for r in refs]


def test_a_phrase_is_worked_through_before_the_next_one_is_picked():
    # Pinned: each request used to draw a new phrase, so a phrase with hundreds of
    # kernels was sampled a page at a time and the model was asked for new phrases
    # while broad ones were still unread.
    api = ByTerm({"outlier": 90, "novelty": 30})
    bank = Terms("outlier", "novelty")
    source = KaggleSource(api, First(), bank)
    for _ in range(3):
        source.fetch(Source.KAGGLE, "Data Science", 30, "cpu", never)

    assert [(c["search"], c["page"]) for c in api.listed] == [("outlier", p) for p in (1, 2, 3)]
    assert bank.picks == 1


def test_a_phrase_that_is_used_up_is_replaced_by_the_next_within_the_same_request():
    api = ByTerm({"outlier": 30, "novelty": 30})
    source = KaggleSource(api, First(), Terms("outlier", "novelty"))
    found = source.fetch(Source.KAGGLE, "Data Science", 60, "cpu", never)

    # The second "outlier" page is the empty one that shows the phrase has ended.
    assert [c["search"] for c in api.listed] == ["outlier", "outlier", "novelty"]
    assert len(found) == 60


def test_a_search_that_holds_all_its_results_is_not_read_again_under_another_sort():
    # Sorting only reorders a search's results, so under a second sort a search of
    # 45 kernels finds the same 45; only a search Kaggle cut off differs by sort.
    api = ByTerm({"outlier": 45})
    source = KaggleSource(api, First(), Terms("outlier"))
    for _ in range(3):
        source.fetch(Source.KAGGLE, "Data Science", 100, "cpu", never)

    assert len({c["sort_by"] for c in api.listed}) == 1
    assert [c["page"] for c in api.listed] == [1, 2]


def test_a_search_cut_off_by_kaggle_is_read_again_under_the_other_sorts():
    api = ByTerm({"outlier": 1200})
    source = KaggleSource(api, First(), Terms("outlier"))
    for _ in range(40):
        source.fetch(Source.KAGGLE, "Data Science", 1000, "cpu", never)

    assert len({c["sort_by"] for c in api.listed}) > 1


def test_an_empty_listing_yields_nothing():
    assert (
        KaggleSource(FakeKaggle(None, {}), First()).fetch(
            Source.KAGGLE, "Data Science", 2, "cpu", never
        )
        == []
    )


class HttpError(Exception):
    """Carries a status code the way ``requests.HTTPError`` does."""

    def __init__(self, status, text=""):
        super().__init__(f"{status} {text}")
        self.response = SimpleNamespace(status_code=status)


class Api:
    def __init__(self, exc=None):
        self.exc = exc
        self.calls: list[str] = []

    def dataset_status(self, slug):
        # Pinned: this answers 404 for every real dataset the caller does not own,
        # which made a live run reject famous datasets as nonexistent.
        self.calls.append("dataset_status")
        raise HttpError(404, "Not Found")

    def dataset_list_files(self, slug):
        self.calls.append("dataset_list_files")
        if self.exc:
            raise self.exc
        return "files"

    def model_get(self, slug):
        self.calls.append("model_get")
        if self.exc:
            raise self.exc
        return object()


def test_kaggle_lookup_reports_existing_resources():
    assert KaggleLookup(Api()).dataset_exists("o/d")
    assert KaggleLookup(Api()).model_exists("o/m")


def test_kaggle_lookup_does_not_use_the_call_that_misreports_public_datasets():
    api = Api()
    KaggleLookup(api).dataset_exists("o/d")
    assert api.calls == ["dataset_list_files"]


@pytest.mark.parametrize("status", [403, 404])
def test_kaggle_lookup_treats_forbidden_and_not_found_as_missing(status):
    # Seen live: Kaggle answers 403 for a dataset or model that does not exist.
    lookup = KaggleLookup(Api(HttpError(status)))
    assert not lookup.dataset_exists("o/d")
    assert not lookup.model_exists("o/m")


@pytest.mark.parametrize("status", [429, 500, 503])
def test_kaggle_lookup_does_not_treat_other_statuses_as_missing(status):
    with pytest.raises(ResourceLookupError):
        KaggleLookup(Api(HttpError(status))).dataset_exists("o/d")


def test_kaggle_lookup_does_not_treat_an_error_without_a_status_as_missing():
    with pytest.raises(ResourceLookupError):
        KaggleLookup(Api(ConnectionError("reset by peer"))).model_exists("o/m")


# ---------------------------------------------------------------------------
# Hugging Face
# ---------------------------------------------------------------------------
class FakeHub:
    """A Hub whose listings carry file names, like ``expand=["siblings"]``."""

    token = None

    def __init__(self, repos):
        self.repos = repos
        self.requests: list[dict[str, Any]] = []

    def _listing(self, **kwargs):
        self.requests.append(kwargs)
        return iter(
            [
                SimpleNamespace(id=name, siblings=[SimpleNamespace(rfilename=f) for f in files])
                for name, files in self.repos.items()
            ]
        )

    list_models = list_datasets = list_spaces = _listing


class Hub(HuggingFaceSource):
    """Serves file contents from a dict instead of downloading them."""

    def __init__(self, api, contents, rng, keywords=None):
        super().__init__(api, rng, keywords)
        self.contents = contents
        self.reads: list[tuple[str, str]] = []

    def _read(self, repo, filename):
        self.reads.append((repo.id, filename))
        return self.contents.get((repo.id, filename))


CARD = (
    "Use it:\n```python\nfrom transformers import pipeline\n"
    "p = pipeline('fill-mask', model='org/m')\nprint(p('Hi [MASK]'))\n```\n"
)
SCRIPT = "import torch\nx = torch.zeros(3)\nprint(x.sum())\n"


def test_a_card_snippet_that_parses_becomes_a_candidate_with_a_ref():
    hub = Hub(FakeHub({"org/m": ["README.md"]}), {("org/m", "README.md"): CARD}, First())
    found = hub.fetch(Source.HUGGINGFACE, "Natural Language Processing", 3, "cpu", never)

    assert "pipeline('fill-mask'" in found[0].code[0]
    assert found[0].ref == "https://huggingface.co/org/m#snippet-0"


def test_the_listing_is_asked_for_file_names_and_a_wide_window():
    api = FakeHub({"org/m": ["README.md"]})
    Hub(api, {}, First()).fetch(Source.HUGGINGFACE, "Data Science", 1, "cpu", never)
    assert api.requests[0]["expand"] == ["siblings"]
    assert api.requests[0]["limit"] >= 300


def test_an_example_script_is_taken_alongside_the_readme():
    files = ["README.md", "inference.py", "setup.py", "tests/test_x.py"]
    hub = Hub(
        FakeHub({"org/m": files}),
        {("org/m", "inference.py"): SCRIPT, ("org/m", "README.md"): CARD},
        First(),
    )
    found = hub.fetch(Source.HUGGINGFACE, "Data Science", 5, "cpu", never)

    assert [c.ref for c in found] == [
        "https://huggingface.co/org/m/blob/main/inference.py",
        "https://huggingface.co/org/m#snippet-0",
    ]
    # Packaging and tests are not workloads and are never downloaded.
    assert ("org/m", "setup.py") not in hub.reads
    assert ("org/m", "tests/test_x.py") not in hub.reads


def test_a_script_that_does_not_parse_is_skipped():
    hub = Hub(
        FakeHub({"org/m": ["app.py"]}), {("org/m", "app.py"): "def broken(:\n  a\n  b"}, First()
    )
    assert hub.fetch(Source.HUGGINGFACE, "Data Science", 3, "cpu", never) == []


def test_a_snippet_that_is_not_python_or_too_short_is_skipped():
    card = "```python\nx = 1\n```\n```python\ndef broken(:\n  pass\n  pass\n```\n"
    hub = Hub(FakeHub({"org/m": ["README.md"]}), {("org/m", "README.md"): card}, First())
    assert hub.fetch(Source.HUGGINGFACE, "Data Science", 3, "cpu", never) == []


NOTEBOOK = json.dumps({"cells": [{"cell_type": "code", "source": ["a = 1"]}]})


def notebook_hub(extra=None):
    repos = {"org/plain": ["README.md"], "org/m": ["README.md", "demo.ipynb"], **(extra or {})}
    return Hub(FakeHub(repos), {("org/m", "demo.ipynb"): NOTEBOOK}, First())


def test_repository_notebooks_become_their_code_cells():
    hub = notebook_hub()
    found = hub.fetch(Source.HUGGINGFACE_NOTEBOOK, "Data Science", 3, "cpu", never)

    assert found[0].code == ("a = 1",)
    assert found[0].ref == "https://huggingface.co/org/m/blob/main/demo.ipynb"


def test_repositories_without_a_notebook_are_skipped_without_a_download():
    # Pinned: the file list comes with the listing, so only repositories that
    # hold a notebook cost a request.
    hub = notebook_hub()
    hub.fetch(Source.HUGGINGFACE_NOTEBOOK, "Data Science", 3, "cpu", never)
    assert hub.reads == [("org/m", "demo.ipynb")]


def test_an_origin_already_seen_is_not_returned_again():
    seen = notebook_hub().fetch(
        Source.HUGGINGFACE_NOTEBOOK, "Data Science", 3, "cpu", lambda _: True
    )
    assert seen == []


def test_a_request_reads_a_bounded_number_of_repositories():
    repos = {f"org/r{i}": ["README.md", "demo.ipynb"] for i in range(200)}
    hub = Hub(FakeHub(repos), {}, First())
    hub.fetch(Source.HUGGINGFACE_NOTEBOOK, "Data Science", 5, "cpu", never)
    assert len(hub.reads) <= 30


def test_repeated_requests_walk_on_through_the_same_search_instead_of_reading_its_head_again():
    # Pinned: each request started at a random offset and read the same few
    # repositories again; a narrow search has a few hundred matches in all.
    repos = {f"org/r{i}": ["README.md"] for i in range(70)}
    hub = Hub(FakeHub(repos), {("org/r" + str(i), "README.md"): CARD for i in range(70)}, First())
    first = hub.fetch(Source.HUGGINGFACE, "Data Science", 100, "cpu", never)
    second = hub.fetch(Source.HUGGINGFACE, "Data Science", 100, "cpu", never)

    assert len(first) == 30  # the per-request read limit
    assert {c.ref for c in first}.isdisjoint(c.ref for c in second)
    assert len(second) == 30


def test_a_search_is_listed_once_however_many_requests_use_it():
    api = FakeHub({"org/m": ["README.md"]})
    hub = Hub(api, {}, First())
    for _ in range(3):
        hub.fetch(Source.HUGGINGFACE, "Data Science", 1, "cpu", never)
    assert len(api.requests) == 1


class Sparse(First):
    """Picks the last search term and always applies the flavor hint."""

    def choice(self, seq):
        return seq[-1]

    def random(self):
        return 0.0


def test_the_search_draws_on_hub_friendly_terms_as_well_as_the_domain_terms():
    from readie_pipeline.corpus.domains import HUB_TERMS, SEARCH_TERMS

    terms = {t for dom in SEARCH_TERMS for t in (*SEARCH_TERMS[dom], *HUB_TERMS[dom])}
    # Pinned: "feature engineering" matches one Hub repository by name, while
    # "sklearn" and "tabular" match many, so the Hub terms must be reachable.
    assert {"sklearn", "tabular", "forecasting", "feature engineering"} <= terms
    assert set(HUB_TERMS) == set(SEARCH_TERMS)


def test_a_flavor_hint_is_added_only_part_of_the_time():
    plain, hinted = FakeHub({"org/m": ["README.md"]}), FakeHub({"org/m": ["README.md"]})
    Hub(plain, {}, First()).fetch(Source.HUGGINGFACE, "Feature Engineering", 1, "cpu", never)
    Hub(hinted, {}, Sparse()).fetch(Source.HUGGINGFACE, "Feature Engineering", 1, "cpu", never)

    assert plain.requests[0]["search"] == "feature engineering"
    assert hinted.requests[0]["search"] == "skops distil"


# ---------------------------------------------------------------------------
# Model-grown search phrases
# ---------------------------------------------------------------------------
class SpyBank:
    """Stands in for a KeywordBank, recording what the sources ask of it."""

    def __init__(self, term="learned phrase", added=3):
        self.term = term
        self.added = added
        self.picks: list[tuple[str, str, str]] = []
        self.broadened: list[tuple[str, str, str, list[str]]] = []

    def pick(self, platform, domain, flavor, rng):
        self.picks.append((platform, domain, flavor))
        return self.term

    def broaden(self, platform, domain, flavor, examples=()):
        self.broadened.append((platform, domain, flavor, list(examples)))
        return self.added


def test_kaggle_searches_with_a_phrase_from_the_bank():
    bank = SpyBank("customer churn")
    api = FakeKaggle([], {})
    KaggleSource(api, First(), bank).fetch(Source.KAGGLE, "Data Science", 1, "cpu", never)

    # A phrase is drawn for each page a request reads, so each page can use a new one.
    assert set(bank.picks) == {("kaggle", "Data Science", "cpu")}
    assert str(api.listed[0]["search"]).startswith("customer churn")


def test_kaggle_broadens_through_the_bank_and_reports_how_many_phrases_were_added():
    bank = SpyBank(added=4)
    got = KaggleSource(FakeKaggle([], {}), First(), bank).broaden(
        Source.KAGGLE, "Data Science", "cpu", ["a", "b"]
    )
    assert got == 4
    assert bank.broadened == [("kaggle", "Data Science", "cpu", ["a", "b"])]


def test_without_a_bank_kaggle_falls_back_to_the_seed_phrases_and_cannot_broaden():
    api = FakeKaggle([], {})
    source = KaggleSource(api, First())
    source.fetch(Source.KAGGLE, "Data Science", 1, "cpu", never)
    assert str(api.listed[0]["search"]).startswith("data science")
    assert source.broaden(Source.KAGGLE, "Data Science", "cpu", []) == 0


def test_hugging_face_searches_with_a_phrase_from_the_bank_and_broadens_through_it():
    bank = SpyBank("llama", added=2)
    api = FakeHub({"org/m": ["README.md"]})
    source = Hub(api, {}, First(), bank)
    source.fetch(Source.HUGGINGFACE, "Data Science", 1, "cpu", never)

    assert bank.picks == [("huggingface", "Data Science", "cpu")]
    assert api.requests[0]["search"] == "llama"
    assert source.broaden(Source.HUGGINGFACE, "Data Science", "cpu", ["x"]) == 2
    assert bank.broadened[0][0] == "huggingface"
