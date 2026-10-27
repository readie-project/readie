"""The planners that share greedy's rules: kmedoids, toptime and the Jaccard pair."""

from __future__ import annotations

import sys

import numpy as np
import pytest

from readie_pipeline.cli import build_planner
from readie_pipeline.config import ConfigError
from readie_pipeline.corpus.models import Corpus
from readie_pipeline.metadata.models import Metadata, PackageFacts
from readie_pipeline.planning._closures import jaccard_distances
from readie_pipeline.planning.greedy import GreedyPlanner
from readie_pipeline.planning.jaccard import JaccardAgglomerativePlanner, JaccardKMedoidsPlanner
from readie_pipeline.planning.kmedoids import KMedoidsPlanner
from readie_pipeline.planning.ports import Budget, CheckpointPlanner
from readie_pipeline.planning.toptime import TopTimePlanner
from tests.helpers import item

ALPHA = 0.005

PLANNER_CLASSES = [
    pytest.param(KMedoidsPlanner, id="kmedoids"),
    pytest.param(TopTimePlanner, id="toptime"),
    pytest.param(JaccardKMedoidsPlanner, id="jaccard-kmedoids"),
    pytest.param(JaccardAgglomerativePlanner, id="jaccard-agglomerative"),
]


def req(*imports: str):
    return item(*imports)


def facts(**sizes: tuple[float, float]) -> Metadata:
    return Metadata(
        {
            name: PackageFacts(
                base_import=name, disk_size_mb=size, memory_size_mb=size, import_time=seconds
            )
            for name, (size, seconds) in sizes.items()
        }
    )


def with_numpy() -> Metadata:
    """Metadata where pandas and scipy each load numpy."""

    def pkg(name: str, size: float, seconds: float, loads: set[str]) -> PackageFacts:
        return PackageFacts(
            base_import=name,
            disk_size_mb=size,
            memory_size_mb=size,
            import_time=seconds,
            loaded_modules=frozenset({name, *loads}),
        )

    return Metadata(
        {
            "pandas": pkg("pandas", 40, 0.3, {"numpy"}),
            "scipy": pkg("scipy", 50, 0.5, {"numpy"}),
            "numpy": pkg("numpy", 20, 0.1, set()),
        }
    )


@pytest.mark.parametrize("cls", PLANNER_CLASSES)
def test_every_planner_satisfies_the_protocol(cls):
    assert isinstance(cls(alpha=ALPHA), CheckpointPlanner)


@pytest.mark.parametrize("cls", PLANNER_CLASSES)
def test_an_empty_corpus_plans_nothing(cls):
    assert cls(alpha=ALPHA).plan(Corpus.of([]), Metadata({}), Budget()) == []


@pytest.mark.parametrize("cls", PLANNER_CLASSES)
def test_the_size_budget_is_shared_across_checkpoints(cls):
    corpus = Corpus.of([req("a")] * 20 + [req("b")] * 20 + [req("c")] * 20)
    metadata = facts(a=(60, 1.0), b=(60, 1.0), c=(60, 1.0))

    plans = cls(alpha=ALPHA).plan(corpus, metadata, Budget(max_checkpoints=10, size_mb=130))

    assert plans
    assert sum(p.size_mb for p in plans) <= 130


@pytest.mark.parametrize("cls", PLANNER_CLASSES)
def test_no_more_checkpoints_than_the_budget_allows(cls):
    corpus = Corpus.of([req("a")] * 20 + [req("b")] * 20 + [req("c")] * 20)
    metadata = facts(a=(10, 1.0), b=(10, 1.0), c=(10, 1.0))

    plans = cls(alpha=ALPHA).plan(corpus, metadata, Budget(max_checkpoints=2, size_mb=500))

    assert len(plans) <= 2


@pytest.mark.parametrize("cls", PLANNER_CLASSES)
def test_a_checkpoint_is_sized_and_charged_for_its_dependencies(cls):
    # The checkpoint holds pandas' whole closure, so numpy is paid for even though only
    # pandas is named in the plan.
    corpus = Corpus.of([req("pandas")] * 50)

    plans = cls(alpha=ALPHA).plan(corpus, with_numpy(), Budget(max_checkpoints=1, size_mb=500))

    assert plans[0].imports == ("pandas",)
    assert plans[0].size_mb == pytest.approx(60)
    assert plans[0].seconds_saved == pytest.approx((0.3 + 0.1) * 50)


@pytest.mark.parametrize("cls", PLANNER_CLASSES)
def test_a_budget_too_small_for_the_dependencies_plans_nothing(cls):
    # pandas alone is 40 MB but needs numpy too: 60 MB. A budget between the two must not
    # produce a checkpoint holding pandas without its dependency.
    corpus = Corpus.of([req("pandas")] * 50)

    plans = cls(alpha=ALPHA).plan(corpus, with_numpy(), Budget(max_checkpoints=1, size_mb=50))

    assert plans == []


@pytest.mark.parametrize("cls", PLANNER_CLASSES)
def test_stdlib_modules_are_never_selected(cls):
    corpus = Corpus.of([req("json", "pandas")] * 50)
    metadata = facts(pandas=(40, 0.3), json=(1, 0.01))

    plans = cls(alpha=ALPHA).plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=500))

    assert set(plans[0].imports).isdisjoint(sys.stdlib_module_names)
    assert "pandas" in plans[0].imports


@pytest.mark.parametrize("cls", PLANNER_CLASSES)
def test_a_package_almost_nobody_uses_is_skipped(cls):
    corpus = Corpus.of([req("common")] * 1000 + [req("rare")])
    metadata = facts(common=(10, 0.5), rare=(1, 5.0))

    plans = cls(alpha=ALPHA).plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=500))

    assert "rare" not in plans[0].imports


@pytest.mark.parametrize("cls", PLANNER_CLASSES)
def test_the_same_inputs_give_the_same_plan(cls):
    corpus = Corpus.of([req("a")] * 7 + [req("b")] * 5 + [req("a", "c")] * 3 + [req("d")] * 4)
    metadata = facts(a=(10, 1.0), b=(12, 0.8), c=(8, 0.6), d=(9, 0.9))
    budget = Budget(max_checkpoints=2, size_mb=40)

    first = cls(alpha=ALPHA).plan(corpus, metadata, budget)
    second = cls(alpha=ALPHA).plan(corpus, metadata, budget)

    assert first == second


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("key", "cls"),
    [
        ("kmedoids", KMedoidsPlanner),
        ("toptime", TopTimePlanner),
        ("jaccard-kmedoids", JaccardKMedoidsPlanner),
        ("jaccard-agglomerative", JaccardAgglomerativePlanner),
    ],
)
def test_a_planner_is_resolved_by_its_key(key, cls):
    assert isinstance(build_planner(key, ALPHA), cls)


def test_an_unknown_planner_lists_the_choices():
    with pytest.raises(ConfigError, match=r"kmedoids.*toptime"):
        build_planner("nope", ALPHA)


# ---------------------------------------------------------------------------
# K-medoids
# ---------------------------------------------------------------------------
def test_kmedoids_is_no_worse_than_greedy_on_the_objective():
    # It starts from the greedy solution and only accepts improving moves.
    corpus = Corpus.of(
        [req("a", "b")] * 6 + [req("b", "c")] * 6 + [req("a", "c")] * 6 + [req("d")] * 5
    )
    metadata = facts(a=(10, 1.0), b=(10, 1.0), c=(10, 1.0), d=(15, 2.0))
    budget = Budget(max_checkpoints=2, size_mb=40)

    def remaining(plans):
        return sum(p.seconds_saved for p in plans)

    kmedoids = KMedoidsPlanner(alpha=ALPHA).plan(corpus, metadata, budget)
    greedy = GreedyPlanner(alpha=ALPHA).plan(corpus, metadata, budget)

    assert remaining(kmedoids) >= remaining(greedy) - 1e-9


def test_kmedoids_escapes_a_greedy_first_pick_that_wastes_the_budget():
    # Greedy takes the big popular closure first and then cannot afford both others;
    # swapping it out serves more. Two medoids of 10 MB beat one of 25 MB.
    corpus = Corpus.of([req("big")] * 10 + [req("x")] * 9 + [req("y")] * 9)
    metadata = facts(big=(25, 1.0), x=(10, 1.0), y=(10, 1.0))
    budget = Budget(max_checkpoints=2, size_mb=25)

    plans = KMedoidsPlanner(alpha=ALPHA).plan(corpus, metadata, budget)

    assert {p.imports for p in plans} == {("x",), ("y",)}


# ---------------------------------------------------------------------------
# Top time
# ---------------------------------------------------------------------------
def test_toptime_takes_the_largest_time_reductions_first():
    corpus = Corpus.of([req("slow")] * 10 + [req("quick")] * 100)
    metadata = facts(slow=(10, 4.0), quick=(10, 0.001))

    plans = TopTimePlanner(alpha=ALPHA).plan(
        corpus, metadata, Budget(max_checkpoints=1, size_mb=10)
    )

    assert plans[0].imports == ("slow",)


def test_toptime_skips_one_that_does_not_fit_and_takes_the_next():
    corpus = Corpus.of([req("huge")] * 10 + [req("small")] * 10)
    metadata = facts(huge=(100, 5.0), small=(10, 1.0))

    plans = TopTimePlanner(alpha=ALPHA).plan(
        corpus, metadata, Budget(max_checkpoints=2, size_mb=50)
    )

    assert [p.imports for p in plans] == [("small",)]


def test_toptime_does_not_discount_overlap_between_its_picks():
    # Unlike greedy, nothing is re-ranked after a pick: the second closure is taken on
    # its standalone saving even though the first already holds most of it.
    corpus = Corpus.of([req("pandas")] * 10 + [req("pandas", "scipy")] * 10)
    metadata = Metadata(
        {
            **with_numpy().packages,
        }
    )

    plans = TopTimePlanner(alpha=ALPHA).plan(
        corpus, metadata, Budget(max_checkpoints=2, size_mb=500)
    )

    assert len(plans) == 2


# ---------------------------------------------------------------------------
# Jaccard
# ---------------------------------------------------------------------------
def test_jaccard_distance_is_one_minus_overlap_over_union():
    dist = jaccard_distances([frozenset("ab"), frozenset("bc"), frozenset("de")])

    assert dist[0, 1] == pytest.approx(1 - 1 / 3)
    assert dist[0, 2] == pytest.approx(1.0)
    assert dist[0, 0] == pytest.approx(0.0)
    assert np.allclose(dist, dist.T)


@pytest.mark.parametrize("cls", [JaccardKMedoidsPlanner, JaccardAgglomerativePlanner])
def test_similar_closures_share_a_checkpoint_and_unrelated_ones_do_not(cls):
    corpus = Corpus.of([req("a", "b")] * 5 + [req("a", "b", "c")] * 5 + [req("x", "y")] * 5)
    metadata = facts(a=(5, 1.0), b=(5, 1.0), c=(5, 1.0), x=(5, 1.0), y=(5, 1.0))

    plans = cls(alpha=ALPHA).plan(corpus, metadata, Budget(max_checkpoints=2, size_mb=500))

    assert {p.imports for p in plans} == {("a", "b", "c"), ("x", "y")}


@pytest.mark.parametrize("cls", [JaccardKMedoidsPlanner, JaccardAgglomerativePlanner])
def test_a_cluster_too_big_for_the_budget_drops_its_lightest_members(cls):
    # a+b+c is 30 MB, over the 25 MB budget, so the rare member is dropped and a+b is kept.
    corpus = Corpus.of([req("a", "b")] * 10 + [req("a", "b", "c")] * 2)
    metadata = facts(a=(10, 1.0), b=(10, 1.0), c=(10, 1.0))

    plans = cls(alpha=ALPHA).plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=25))

    assert [p.imports for p in plans] == [("a", "b")]


def test_agglomerative_weights_linkage_by_occurrence():
    # Three closures in a line: b is close to both ends. With many requests on "a b" it pulls
    # "b c" toward it, so the two clusters are {a b, b c} and {x y}.
    planner = JaccardAgglomerativePlanner(alpha=ALPHA)
    closures = [frozenset("ab"), frozenset("bc"), frozenset("xy")]

    clusters = planner._cluster(jaccard_distances(closures), np.asarray([9.0, 1.0, 1.0]), 2)

    assert sorted(clusters) == [[0, 1], [2]]
