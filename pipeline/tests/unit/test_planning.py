"""Checkpoint selection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from crfs_pipeline.corpus.models import Corpus, Request
from crfs_pipeline.metadata.models import Metadata, PackageFacts
from crfs_pipeline.planning.fixed import FixedPlanner
from crfs_pipeline.planning.greedy import GreedyPlanner
from crfs_pipeline.planning.ports import Budget, CheckpointPlan, CheckpointPlanner

DATA = Path(__file__).parents[2] / "data"


def req(*imports: str) -> Request:
    return Request(task_name="t", category="c", code="", imports=imports)


def facts(**sizes: tuple[float, float]) -> Metadata:
    """metadata(name=(size_mb, import_seconds))."""
    return Metadata(
        {
            name: PackageFacts(base_import=name, disk_size_mb=size, import_time=seconds)
            for name, (size, seconds) in sizes.items()
        }
    )


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("planner", [FixedPlanner(), GreedyPlanner()])
def test_every_planner_satisfies_the_protocol(planner):
    assert isinstance(planner, CheckpointPlanner)


@pytest.mark.parametrize("planner", [FixedPlanner(), GreedyPlanner()])
def test_an_empty_corpus_plans_nothing_rather_than_crashing(planner):
    plans = planner.plan(Corpus.of([]), Metadata({}), Budget())
    assert all(isinstance(p, CheckpointPlan) for p in plans)


def test_the_fixed_planner_reproduces_the_previous_behaviour():
    plans = FixedPlanner().plan(Corpus.of([req("pandas")]), Metadata({}), Budget())
    assert [p.imports for p in plans] == [("pandas", "numpy")]


def test_the_fixed_planner_still_scores_against_the_corpus():
    # A fixed plan and a computed one must be comparable on the same numbers,
    # which is the only way to tell whether the computed one is better.
    corpus = Corpus.of([req("pandas"), req("numpy"), req("torch")])
    plans = FixedPlanner().plan(corpus, facts(pandas=(40, 0.3), numpy=(20, 0.1)), Budget())

    assert plans[0].requests_served == 2
    assert plans[0].seconds_saved == pytest.approx(0.4)
    assert plans[0].size_mb == pytest.approx(60)


# ---------------------------------------------------------------------------
# Greedy
# ---------------------------------------------------------------------------
def test_it_prefers_import_time_over_popularity():
    # Saving four seconds for a few requests beats milliseconds for many, which
    # is the whole reason the objective is time rather than a request count.
    corpus = Corpus.of([req("slow")] * 10 + [req("quick")] * 100)
    metadata = facts(slow=(10, 4.0), quick=(10, 0.001))

    plans = GreedyPlanner().plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=10))
    assert plans[0].imports == ("slow",)


def test_it_prefers_the_cheaper_of_two_equally_useful_packages():
    corpus = Corpus.of([req("big", "small")] * 10)
    metadata = facts(big=(500, 1.0), small=(5, 1.0))

    plans = GreedyPlanner().plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=100))
    assert plans[0].imports == ("small",)


def test_it_respects_the_size_budget():
    corpus = Corpus.of([req("a", "b", "c")] * 20)
    metadata = facts(a=(60, 1.0), b=(60, 1.0), c=(60, 1.0))

    plans = GreedyPlanner().plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=130))
    assert plans[0].size_mb <= 130
    assert len(plans[0].imports) == 2


def test_later_checkpoints_specialise_rather_than_repeat():
    # A request restores exactly one checkpoint, so a second copy of the best
    # set adds nothing. Ranking by absolute value -- the obvious first attempt
    # -- produces K identical plans.
    corpus = Corpus.of([req("pandas")] * 100 + [req("torch")] * 40)
    metadata = facts(pandas=(40, 0.3), torch=(80, 2.0))

    plans = GreedyPlanner().plan(corpus, metadata, Budget(max_checkpoints=2, size_mb=80))

    assert len(plans) == 2
    assert set(plans[0].imports) != set(plans[1].imports)
    assert {"pandas"} in [set(p.imports) for p in plans]
    assert {"torch"} in [set(p.imports) for p in plans]


def test_it_stops_once_nothing_improves_on_what_is_already_planned():
    # Asking for eight checkpoints of a corpus that needs one must not produce
    # eight copies of it.
    corpus = Corpus.of([req("pandas")] * 50)
    metadata = facts(pandas=(40, 0.3))

    plans = GreedyPlanner().plan(corpus, metadata, Budget(max_checkpoints=8, size_mb=500))
    assert len(plans) == 1


def test_stdlib_modules_are_never_selected():
    # They install nothing and cost microseconds, but with no measured import
    # time they fall back to a default that, multiplied across thousands of
    # requests, outranks packages that matter. `json` won a slot this way.
    corpus = Corpus.of([req("json", "collections", "pandas")] * 50)
    metadata = facts(pandas=(40, 0.3))

    plans = GreedyPlanner().plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=500))

    assert set(plans[0].imports).isdisjoint(sys.stdlib_module_names)
    assert "pandas" in plans[0].imports


def test_a_package_almost_nobody_uses_is_skipped():
    corpus = Corpus.of([req("common")] * 1000 + [req("rare")])
    metadata = facts(common=(10, 0.5), rare=(1, 5.0))

    plans = GreedyPlanner().plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=500))
    assert "rare" not in plans[0].imports


def test_a_package_with_no_measurement_ranks_below_a_measured_one():
    # An unmeasured package is charged a nominal saving, so it is not treated as
    # worthless -- but it must never outrank one that was actually profiled.
    corpus = Corpus.of([req("measured", "unmeasured")] * 50)
    metadata = facts(measured=(10, 1.0))

    # A budget that fits exactly one of them: the measured one has to win it.
    plans = GreedyPlanner().plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=10))
    assert plans[0].imports == ("measured",)


def test_the_reported_saving_is_undiscounted_so_it_can_be_checked():
    # What this checkpoint saves if a request lands on it -- a number an
    # operator can compare against a measured cold start.
    corpus = Corpus.of([req("a")] * 10)
    metadata = facts(a=(10, 2.0))

    plans = GreedyPlanner().plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=50))
    assert plans[0].seconds_saved == pytest.approx(20.0)
    assert plans[0].requests_served == 10


def test_alpha_is_the_size_vs_time_threshold():
    # One request needing a 10 MB package that saves 0.05 s: worth 0.005 s/MB.
    # A low alpha admits it; an alpha above that rate rejects it. This is the
    # knob the router shares to select the checkpoint that minimises the same
    # alpha*size + residual cost.
    corpus = Corpus.of([req("borderline")])
    metadata = facts(borderline=(10, 0.05))
    budget = Budget(max_checkpoints=1, size_mb=50)

    lenient = GreedyPlanner(alpha=0.002).plan(corpus, metadata, budget)
    strict = GreedyPlanner(alpha=0.01).plan(corpus, metadata, budget)

    assert lenient
    assert lenient[0].imports == ("borderline",)
    assert strict == [], "nothing clears the stricter weight"


def test_imports_are_sorted_so_a_plan_diffs_cleanly():
    corpus = Corpus.of([req("zeta", "alpha")] * 50)
    metadata = facts(zeta=(10, 1.0), alpha=(10, 1.0))

    plans = GreedyPlanner().plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=50))
    assert list(plans[0].imports) == sorted(plans[0].imports)


# ---------------------------------------------------------------------------
# Against the committed corpus
# ---------------------------------------------------------------------------
def real_inputs() -> tuple[Corpus, Metadata]:
    return Corpus.load(DATA / "dataset.json"), Metadata.load(DATA / "metadata.json")


def test_the_plan_is_deterministic_across_runs():
    # No randomness and every tie breaks on the package name, so a plan can be
    # reviewed in a diff and pinned by a test.
    corpus, metadata = real_inputs()
    budget = Budget(max_checkpoints=4, size_mb=400)

    first = [p.to_json() for p in GreedyPlanner().plan(corpus, metadata, budget)]
    second = [p.to_json() for p in GreedyPlanner().plan(corpus, metadata, budget)]

    assert first == second


def test_it_beats_the_fixed_plan_on_the_real_corpus():
    # The bar the greedy planner has to clear: the hardcoded pandas+numpy set.
    corpus, metadata = real_inputs()
    budget = Budget(max_checkpoints=1, size_mb=400)

    greedy = GreedyPlanner().plan(corpus, metadata, budget)[0]
    fixed = FixedPlanner().plan(corpus, metadata, budget)[0]

    assert greedy.requests_served > fixed.requests_served
    assert greedy.seconds_saved > fixed.seconds_saved


def test_a_tighter_budget_produces_more_specialised_checkpoints():
    corpus, metadata = real_inputs()

    roomy = GreedyPlanner().plan(corpus, metadata, Budget(max_checkpoints=4, size_mb=4096))
    tight = GreedyPlanner().plan(corpus, metadata, Budget(max_checkpoints=4, size_mb=300))

    # Everything useful fits in one checkpoint when the budget is large, so a
    # second adds nothing; when it does not fit, the plan splits.
    assert len(roomy) < len(tight)


def test_every_planned_checkpoint_stays_within_its_budget():
    corpus, metadata = real_inputs()
    budget = Budget(max_checkpoints=4, size_mb=300)

    for plan in GreedyPlanner().plan(corpus, metadata, budget):
        assert plan.size_mb <= budget.size_mb


def test_the_plan_round_trips_through_json():
    # Through the JSON form, not back to the object: to_json rounds the scores
    # so a regenerated plan diffs cleanly, and that rounding is deliberate.
    corpus, metadata = real_inputs()
    plans = GreedyPlanner().plan(corpus, metadata, Budget(max_checkpoints=2, size_mb=400))

    encoded = [p.to_json() for p in plans]
    assert [CheckpointPlan.from_json(e).to_json() for e in encoded] == encoded
