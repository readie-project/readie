import random
from collections import Counter
from typing import Any

from readie_pipeline.corpus.models import Corpus, Source
from readie_pipeline.corpus.popularity import TIER_WEIGHTS, Tier, pick_tier
from readie_pipeline.corpus.sources import agent as agent_module
from readie_pipeline.corpus.sources.agent import AgentSource
from readie_pipeline.corpus.sources.huggingface import _OFFSETS, HuggingFaceSource
from readie_pipeline.corpus.sources.kaggle import _PAGES, KaggleSource
from tests.helpers import item


def never(_ref):
    return False


class Fixed(random.Random):
    """Forces the tier choice and takes the first option elsewhere."""

    def __init__(self, tier: Tier):
        super().__init__(0)
        self.tier = tier

    def choices(self, population, weights=None, **_):
        return [self.tier]

    def choice(self, seq):
        return seq[0]

    def randint(self, a, b):
        return a

    def sample(self, population, k, **_):
        return list(population)[:k]


def test_mainstream_is_the_largest_tier_and_the_weights_sum_to_one():
    assert max(TIER_WEIGHTS, key=lambda t: TIER_WEIGHTS[t]) is Tier.MAINSTREAM
    assert abs(sum(TIER_WEIGHTS.values()) - 1) < 1e-9


def test_picked_tiers_follow_the_weights():
    rng = random.Random(0)
    counts = Counter(pick_tier(rng) for _ in range(4000))
    for tier, weight in TIER_WEIGHTS.items():
        assert abs(counts[tier] / 4000 - weight) < 0.03


def test_kaggle_asks_the_top_pages_for_mainstream_and_deep_pages_for_niche():
    seen: dict[Tier, dict[str, Any]] = {}

    class Api:
        tier: Tier

        def kernels_list(self, **kw):
            seen.setdefault(self.tier, kw)  # a request may read several pages

    for tier in Tier:
        api = Api()
        api.tier = tier
        KaggleSource(api, Fixed(tier)).fetch(Source.KAGGLE, "Data Science", 1, "cpu", never)
    assert seen[Tier.MAINSTREAM]["page"] == _PAGES[Tier.MAINSTREAM][0] == 1
    assert seen[Tier.MAINSTREAM]["sort_by"] == "voteCount"
    assert seen[Tier.NICHE]["page"] > seen[Tier.COMMON]["page"] > seen[Tier.MAINSTREAM]["page"]
    assert seen[Tier.NICHE]["sort_by"] != "voteCount"


def test_hugging_face_ranks_by_downloads_at_the_top_for_mainstream():
    from types import SimpleNamespace

    asked: dict[Tier, dict[str, Any]] = {}
    first_read: dict[Tier, str] = {}

    class Hub:
        token = None
        tier: Tier

        def list_models(self, **kw):
            asked.setdefault(self.tier, kw)
            return iter(
                SimpleNamespace(id=f"org/r{i}", siblings=[SimpleNamespace(rfilename="a.ipynb")])
                for i in range(1000)
            )

        list_datasets = list_spaces = list_models

    class Reading(HuggingFaceSource):
        tier: Tier

        def _notebooks(self, repo):
            first_read.setdefault(self.tier, repo.id)
            return []

    for tier in Tier:
        hub = Hub()
        hub.tier = tier
        source = Reading(hub, Fixed(tier))
        source.tier = tier
        source.fetch(Source.HUGGINGFACE_NOTEBOOK, "Data Science", 1, "cpu", never)
    assert asked[Tier.MAINSTREAM]["sort"] == "downloads"
    assert _OFFSETS[Tier.MAINSTREAM][0] == 0
    # The tier sets where in the ranking a request starts reading.
    ranks = {t: int(first_read[t].removeprefix("org/r")) for t in Tier}
    assert ranks[Tier.MAINSTREAM] < ranks[Tier.COMMON] < ranks[Tier.NICHE]


class Chat:
    def __init__(self):
        self.users = []

    def complete(self, system, user):
        self.users.append(user)
        return '{"items": []}'


def test_the_agent_prompt_states_how_mainstream_the_code_should_be():
    for tier, phrase in (
        (Tier.MAINSTREAM, "MOST widely used"),
        (Tier.COMMON, "day-to-day professional work"),
        (Tier.NICHE, "less common but still real"),
    ):
        chat = Chat()
        AgentSource(chat, Fixed(tier)).fetch(Source.AGENT_SHORT, "Data Science", 1, "cpu", never)
        assert phrase in chat.users[0]


def test_the_system_prompt_no_longer_steers_away_from_common_code():
    assert "avoid the most obvious" not in agent_module._SYSTEM


def test_package_usage_counts_items_by_top_level_package_and_skips_the_stdlib():
    corpus = Corpus.of(
        [
            item("pandas", "scipy.signal.butter", "json"),
            item("pandas", "scipy.signal.welch"),
            item("rare_pkg"),
        ]
    )
    assert corpus.package_usage() == {"pandas": 2, "scipy": 2, "rare_pkg": 1}
