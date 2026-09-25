from __future__ import annotations

import json
import random

from readie_evals.agent.ports import Fetcher
from readie_evals.build import build_corpus, sample_corpus
from readie_evals.config import Settings
from readie_evals.corpus import CorpusStore
from readie_evals.models import RawCandidate, Task
from tests.fakes.fetcher import FakeFetcher
from tests.fakes.llm import FakeLLM

_GOOD = "def task():\n    return sum(range(5))\n"


def _settings(tmp_path):
    return Settings.from_env(env={"READIE_EVALS_OUT_DIR": str(tmp_path)})


def test_build_corpus_generates_and_adapts_across_sources(tmp_path):
    settings = _settings(tmp_path)
    # One generate response (a snippets list) then one adapt response (a task object).
    llm = FakeLLM(
        [
            json.dumps({"snippets": [{"code": _GOOD, "packages": [], "memory": None}]}),
            json.dumps({"code": _GOOD.replace("5", "6"), "packages": [], "memory": None}),
        ],
    )
    fetchers: dict[str, Fetcher] = {
        "kaggle": FakeFetcher(
            "kaggle",
            {"ML": [RawCandidate(source="kaggle", category="ML", raw_code="messy")]},
        ),
    }
    corpus = CorpusStore(settings.corpus_path)
    added = build_corpus(
        settings,
        llm,
        fetchers,
        corpus,
        sources=["generated", "kaggle"],
        categories=["ML"],
        on_line=lambda _line: None,
    )
    assert added == 2
    sources = {task.source for task in corpus.load()}
    assert sources == {"generated", "kaggle"}


def test_build_corpus_is_resumable_and_fills_only_the_deficit(tmp_path):
    settings = _settings(tmp_path)
    fetchers: dict[str, Fetcher] = {}

    def run_once(response_code):
        llm = FakeLLM([json.dumps({"snippets": [{"code": response_code, "packages": []}]})])
        return build_corpus(
            settings,
            llm,
            fetchers,
            CorpusStore(settings.corpus_path),
            sources=["generated"],
            categories=["ML"],
            on_line=lambda _line: None,
        )

    # per_cell defaults to 3; two runs adding one distinct task each stays under target.
    assert run_once(_GOOD) == 1
    assert run_once(_GOOD.replace("5", "7")) == 1
    assert len(CorpusStore(settings.corpus_path).load()) == 2


def test_build_corpus_routes_session_sources_to_generate_session_tasks(tmp_path):
    settings = Settings.from_env(
        env={"READIE_EVALS_OUT_DIR": str(tmp_path), "READIE_EVALS_PER_CELL": "1"},
    )
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "sessions": [
                        {
                            "cells": [_GOOD, _GOOD.replace("5", "6")],
                            "packages": [],
                            "memory": None,
                        },
                    ],
                },
            ),
        ],
    )
    corpus = CorpusStore(settings.corpus_path)
    added = build_corpus(
        settings,
        llm,
        {},
        corpus,
        sources=["generated-notebook"],
        categories=["ML"],
        on_line=lambda _line: None,
    )
    assert added == 1
    task = corpus.load()[0]
    assert task.source == "generated-notebook"
    assert task.session_kind == "notebook"
    assert task.is_session


def test_sample_corpus_picks_distinct_pairs_and_ignores_existing_counts(tmp_path):
    settings = _settings(tmp_path)
    corpus = CorpusStore(settings.corpus_path)
    # Pre-seed "generated/ML" so a deficit-fill approach might see it as satisfied;
    # sample_corpus should still ask for a fresh one since it never checks counts.
    corpus.append([Task(source="generated", category="ML", code=(_GOOD,))])

    llm = FakeLLM(
        [
            json.dumps({"snippets": [{"code": _GOOD.replace("5", "6"), "packages": []}]}),
            json.dumps({"snippets": [{"code": _GOOD.replace("5", "7"), "packages": []}]}),
        ],
    )
    added = sample_corpus(
        settings,
        llm,
        {},
        corpus,
        sources=["generated"],
        categories=["ML", "NLP"],
        count=2,
        rng=random.Random(0),  # noqa: S311
        on_line=lambda _line: None,
    )
    assert added == 2
    pairs = {(task.source, task.category) for task in corpus.load()}
    assert pairs == {("generated", "ML"), ("generated", "NLP")}
    assert len(corpus.load()) == 3  # the pre-seeded task plus the two sampled ones


def test_build_corpus_routes_real_session_sources_to_adapt_session_candidate(tmp_path):
    settings = Settings.from_env(
        env={"READIE_EVALS_OUT_DIR": str(tmp_path), "READIE_EVALS_PER_CELL": "1"},
    )
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "cells": [_GOOD, _GOOD.replace("5", "6")],
                    "packages": [],
                    "memory": None,
                },
            ),
        ],
    )
    fetchers: dict[str, Fetcher] = {
        "kaggle": FakeFetcher(
            "kaggle",
            {
                "ML": [
                    RawCandidate(
                        source="kaggle",
                        category="ML",
                        raw_code="print(1)\n\nprint(2)",
                        raw_cells=("print(1)", "print(2)"),
                        slug="someuser/some-kernel",
                        provenance="https://www.kaggle.com/code/someuser/some-kernel",
                    ),
                ],
            },
        ),
    }
    corpus = CorpusStore(settings.corpus_path)
    added = build_corpus(
        settings,
        llm,
        fetchers,
        corpus,
        sources=["kaggle-notebook"],
        categories=["ML"],
        on_line=lambda _line: None,
    )
    assert added == 1
    task = corpus.load()[0]
    assert task.source == "kaggle-notebook"
    assert task.session_kind == "notebook"
    assert task.is_session
    assert task.slug == "someuser/some-kernel"


def test_sample_corpus_gives_up_after_max_attempts_when_a_pair_keeps_failing(tmp_path):
    """A pair that never stops failing must not spin the sample forever.

    A pair may be drawn again in a later round (see the "draws the same pair
    more than once" test below), so running out of *distinct* pairs is no longer
    what stops a sample -- this is the other stopping condition.
    """
    settings = _settings(tmp_path)
    corpus = CorpusStore(settings.corpus_path)
    # Only the first draw succeeds; every draw after it gets FakeLLM's "{}"
    # fallback, which generate_tasks treats as unusable.
    llm = FakeLLM([json.dumps({"snippets": [{"code": _GOOD, "packages": []}]})])
    added = sample_corpus(
        settings,
        llm,
        {},
        corpus,
        sources=["generated"],
        categories=["ML"],
        count=5,
        rng=random.Random(0),  # noqa: S311
        on_line=lambda _line: None,
    )
    assert added == 1


def test_sample_corpus_draws_the_same_pair_more_than_once(tmp_path):
    """No repetition means no repeated code, not one draw per pair.

    A single (source, category) pair should be able to supply every requested
    task, as long as each draw's own code differs.
    """
    settings = _settings(tmp_path)
    corpus = CorpusStore(settings.corpus_path)
    llm = FakeLLM(
        [
            json.dumps({"snippets": [{"code": _GOOD, "packages": []}]}),
            json.dumps({"snippets": [{"code": _GOOD.replace("5", "6"), "packages": []}]}),
            json.dumps({"snippets": [{"code": _GOOD.replace("5", "7"), "packages": []}]}),
        ],
    )
    added = sample_corpus(
        settings,
        llm,
        {},
        corpus,
        sources=["generated"],
        categories=["ML"],
        count=3,
        rng=random.Random(0),  # noqa: S311
        on_line=lambda _line: None,
    )
    assert added == 3
    tasks = corpus.load()
    assert {task.source for task in tasks} == {"generated"}
    assert {task.category for task in tasks} == {"ML"}
    assert len({task.id for task in tasks}) == 3, "each draw must have produced distinct code"


def test_sample_corpus_recovers_from_an_unexpected_exception(tmp_path):
    """A single bad draw must not take down every pair sampled after it.

    An LLM hiccup, or -- observed from the kaggle client -- a dependency that
    calls sys.exit() instead of raising, are both meant to be recoverable.
    """

    class _FlakyLLM:
        def __init__(self, responses):
            self._responses = list(responses)
            self._calls = 0

        def complete(self, *, system, user):
            self._calls += 1
            if self._calls == 1:
                raise SystemExit(1)
            return self._responses.pop(0)

    settings = _settings(tmp_path)
    corpus = CorpusStore(settings.corpus_path)
    llm = _FlakyLLM([json.dumps({"snippets": [{"code": _GOOD, "packages": []}]})])
    added = sample_corpus(
        settings,
        llm,
        {},
        corpus,
        sources=["generated"],
        categories=["ML"],
        count=1,
        rng=random.Random(0),  # noqa: S311
        on_line=lambda _line: None,
    )
    assert added == 1
