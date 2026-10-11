import random

import pytest

from readie_pipeline.corpus.domains import DOMAINS
from readie_pipeline.corpus.mix import (
    DEFAULT_WEIGHTS,
    parse_weights,
    remaining,
    schedule,
    source_targets,
)
from readie_pipeline.corpus.models import CorpusError, Source


def test_targets_sum_to_the_total_and_follow_the_weights():
    counts = source_targets(1000, DEFAULT_WEIGHTS)
    assert sum(counts.values()) == 1000
    assert counts[Source.KAGGLE] > counts[Source.AGENT_FIX]


def test_a_zero_weight_disables_a_source():
    weights = parse_weights("agent_fix=0,kaggle=0")
    assert Source.AGENT_FIX not in source_targets(100, weights)
    assert Source.KAGGLE not in source_targets(100, weights)


def test_weights_override_the_defaults_by_name():
    assert parse_weights("kaggle=5")[Source.KAGGLE] == 5
    assert parse_weights("")[Source.HUGGINGFACE] == DEFAULT_WEIGHTS[Source.HUGGINGFACE]


@pytest.mark.parametrize("text", ["blog=1", "kaggle=lots", "kaggle=-1"])
def test_bad_weights_are_rejected(text):
    with pytest.raises(CorpusError):
        parse_weights(text)


def test_every_zero_is_rejected():
    zeroed = ",".join(f"{s.value}=0" for s in Source)
    with pytest.raises(CorpusError, match="zero"):
        parse_weights(zeroed)


def test_every_domain_appears_for_every_source_given_enough_items():
    plan = schedule(7 * len(DOMAINS) * 2, DEFAULT_WEIGHTS, DOMAINS, seed=1)
    for source in Source:
        domains = {d for (d, s), n in plan.items() if s is source and n > 0}
        assert domains == set(DOMAINS)


def test_a_seed_makes_the_schedule_repeatable():
    assert schedule(300, DEFAULT_WEIGHTS, DOMAINS, 7) == schedule(300, DEFAULT_WEIGHTS, DOMAINS, 7)


def test_only_pairs_short_of_target_remain():
    targets = {("Data Science", Source.KAGGLE): 3, ("Computer Vision", Source.KAGGLE): 2}
    have = {("Data Science", Source.KAGGLE): 3, ("Computer Vision", Source.KAGGLE): 1}
    assert remaining(targets, have, random.Random(0)) == [(("Computer Vision", Source.KAGGLE), 1)]
