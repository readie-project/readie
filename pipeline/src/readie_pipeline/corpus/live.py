"""Wire the real services into a generation run.

Kept apart from :mod:`generate` so that module imports none of the optional
extra, and tests build a ``Generator`` from fakes.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from readie_pipeline.config import CorpusSettings
from readie_pipeline.corpus.domains import HUB_TERMS, SEARCH_TERMS
from readie_pipeline.corpus.flavor import FlavorJudge
from readie_pipeline.corpus.generate import require_credentials
from readie_pipeline.corpus.keywords import KeywordBank
from readie_pipeline.corpus.llm import AnthropicChat
from readie_pipeline.corpus.models import Source
from readie_pipeline.corpus.sources import CodeSource
from readie_pipeline.corpus.sources.agent import AgentSource
from readie_pipeline.corpus.standins import StandIns
from readie_pipeline.corpus.verify import LiveChecker, ResourceVerifier


@dataclass(frozen=True, slots=True)
class Live:
    """The real collaborators of a run."""

    sources: dict[Source, CodeSource]
    judge: FlavorJudge
    verifier: ResourceVerifier
    standins: StandIns | None = None


def build(
    settings: CorpusSettings,
    weights: Mapping[Source, float],
    cache_path: Path,
    seed: int | None = None,
    known_packages: frozenset[str] = frozenset(),
) -> Live:
    """Construct the sources, judge and verifier against live services."""
    require_credentials(settings, weights)
    rng = random.Random(seed)  # noqa: S311 - not security sensitive
    chat = AnthropicChat(settings)
    keywords = KeywordBank(
        {
            "kaggle": SEARCH_TERMS,
            "huggingface": {d: (*SEARCH_TERMS[d], *HUB_TERMS[d]) for d in SEARCH_TERMS},
        },
        chat,
        cache_path.with_name("keywords.json"),
    )
    active = {s for s, w in weights.items() if w > 0}

    sources: dict[Source, CodeSource] = {}
    kaggle_lookup = None
    if active & {Source.KAGGLE, Source.KAGGLE_NOTEBOOK}:
        from readie_pipeline.corpus.sources.kaggle import (  # noqa: PLC0415 - optional extra
            KaggleLookup,
            KaggleSource,
            make_api,
        )
        from readie_pipeline.corpus.sources.pacing import Pacer  # noqa: PLC0415

        api = make_api(settings.kaggle_token or "")
        pacer = Pacer(on_line=print)  # one request budget for searches, downloads and checks
        kaggle = KaggleSource(api, rng, keywords, pacer)
        kaggle_lookup = KaggleLookup(api, pacer)
        sources[Source.KAGGLE] = kaggle
        sources[Source.KAGGLE_NOTEBOOK] = kaggle
    if active & {Source.HUGGINGFACE, Source.HUGGINGFACE_NOTEBOOK}:
        from huggingface_hub import HfApi  # noqa: PLC0415 - optional extra

        from readie_pipeline.corpus.sources.huggingface import (  # noqa: PLC0415
            HuggingFaceSource,
        )

        hub = HuggingFaceSource(HfApi(token=settings.hf_token), rng, keywords)
        sources[Source.HUGGINGFACE] = hub
        sources[Source.HUGGINGFACE_NOTEBOOK] = hub

    agent = AgentSource(chat, rng)
    for source in (Source.AGENT_SHORT, Source.AGENT_NOTEBOOK, Source.AGENT_FIX):
        sources[source] = agent

    checker = LiveChecker(kaggle_lookup, settings.hf_token)
    verifier = ResourceVerifier(checker, cache_path, known_packages=known_packages)
    standins = StandIns(chat, max_chars=settings.rewrite_max_chars) if settings.rewrite else None
    return Live(sources, FlavorJudge(chat), verifier, standins)


def build_verifier(
    settings: CorpusSettings, cache_path: Path, known_packages: frozenset[str] = frozenset()
) -> ResourceVerifier:
    """A verifier alone, for re-checking a corpus that already exists."""
    lookup = None
    if settings.kaggle_token:
        from readie_pipeline.corpus.sources.kaggle import (  # noqa: PLC0415 - optional extra
            KaggleLookup,
            make_api,
        )

        lookup = KaggleLookup(make_api(settings.kaggle_token))
    checker = LiveChecker(lookup, settings.hf_token)
    return ResourceVerifier(checker, cache_path, known_packages=known_packages)


def build_standins(settings: CorpusSettings) -> StandIns:
    """The stand-in rewriter alone, for cleaning a corpus that already exists."""
    return StandIns(AnthropicChat(settings), max_chars=settings.rewrite_max_chars)
