"""Checkpoint selection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from readie_pipeline.corpus.models import Corpus, Request
from readie_pipeline.metadata.models import Metadata, PackageFacts
from readie_pipeline.planning.fixed import FixedPlanner
from readie_pipeline.planning.greedy import GreedyPlanner
from readie_pipeline.planning.ports import Budget, CheckpointPlan, CheckpointPlanner

DATA = Path(__file__).parents[2] / "data"


def req(*imports: str) -> Request:
    return Request(task_name="t", category="c", code="", imports=imports)


def facts(**sizes: tuple[float, float]) -> Metadata:
    """metadata(name=(size_mb, import_seconds)).

    Sets both disk and memory size to the same value: the planner scores
    memory_size_mb, but these fixtures don't care about the distinction.
    """
    return Metadata(
        {
            name: PackageFacts(
                base_import=name, disk_size_mb=size, memory_size_mb=size, import_time=seconds
            )
            for name, (size, seconds) in sizes.items()
        }
    )


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("planner", [FixedPlanner(), GreedyPlanner(alpha=0.005)])
def test_every_planner_satisfies_the_protocol(planner):
    assert isinstance(planner, CheckpointPlanner)


@pytest.mark.parametrize("planner", [FixedPlanner(), GreedyPlanner(alpha=0.005)])
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

    plans = GreedyPlanner(alpha=0.005).plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=10))
    assert plans[0].imports == ("slow",)


def test_it_prefers_the_cheaper_of_two_equally_useful_packages():
    # A facility is a whole request's import set, not a single package, so
    # "big" and "small" must be requested separately for either to be
    # selectable on its own. Sized so either fits the budget alone but not
    # together (60 + 5 > 60), forcing a real choice between two packages that
    # save the same total time.
    corpus = Corpus.of([req("big")] * 10 + [req("small")] * 10)
    metadata = facts(big=(60, 1.0), small=(5, 1.0))

    plans = GreedyPlanner(alpha=0.005).plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=60))
    assert plans[0].imports == ("small",)


def test_it_respects_the_size_budget():
    # size_mb is a total shared across every checkpoint, not a per-checkpoint
    # allowance: a, b and c are equally good on their own (60 MB each), so only
    # two of the three fit in a 130 MB total before the budget is spent.
    corpus = Corpus.of([req("a")] * 20 + [req("b")] * 20 + [req("c")] * 20)
    metadata = facts(a=(60, 1.0), b=(60, 1.0), c=(60, 1.0))

    plans = GreedyPlanner(alpha=0.005).plan(
        corpus, metadata, Budget(max_checkpoints=10, size_mb=130)
    )
    assert sum(p.size_mb for p in plans) <= 130
    assert len(plans) == 2


def test_it_stops_once_nothing_improves_on_what_is_already_planned():
    # Asking for eight checkpoints of a corpus that needs one must not produce
    # eight copies of it.
    corpus = Corpus.of([req("pandas")] * 50)
    metadata = facts(pandas=(40, 0.3))

    plans = GreedyPlanner(alpha=0.005).plan(
        corpus, metadata, Budget(max_checkpoints=8, size_mb=500)
    )
    assert len(plans) == 1


def test_stdlib_modules_are_never_selected():
    # Given real, non-zero, attractively cheap facts here (rather than left
    # unmeasured) specifically to prove the STDLIB filter itself excludes
    # them on principle -- not just an accident of them measuring as free.
    corpus = Corpus.of([req("json", "collections", "pandas")] * 50)
    metadata = facts(pandas=(40, 0.3), json=(1, 0.01), collections=(1, 0.01))

    plans = GreedyPlanner(alpha=0.005).plan(
        corpus, metadata, Budget(max_checkpoints=1, size_mb=500)
    )

    assert set(plans[0].imports).isdisjoint(sys.stdlib_module_names)
    assert "pandas" in plans[0].imports


def test_a_package_almost_nobody_uses_is_skipped():
    corpus = Corpus.of([req("common")] * 1000 + [req("rare")])
    metadata = facts(common=(10, 0.5), rare=(1, 5.0))

    plans = GreedyPlanner(alpha=0.005).plan(
        corpus, metadata, Budget(max_checkpoints=1, size_mb=500)
    )
    assert "rare" not in plans[0].imports


def test_a_package_with_no_measurement_ranks_below_a_measured_one():
    # An unmeasured package costs and saves exactly 0 -- no nominal floor on
    # either side -- so it never has anything to offer the objective over a
    # package that was actually profiled and has real savings to show for it.
    # `max_checkpoints=1` forces a single winner between the two facilities.
    #
    # "unmeasured" is given an explicit `resolved` entry but no facts (as a
    # real analyze() run would record it if resolution succeeded but the
    # profiling subprocess itself failed, or if it was folded into an
    # already-measured ancestor's own closure and never independently
    # remeasured -- see `metadata/analyze.py`'s `_visit`) -- distinct from a
    # name that never resolved at all and is dropped before ever reaching
    # the planner.
    corpus = Corpus.of([req("measured")] * 50 + [req("unmeasured")] * 50)
    metadata = Metadata(
        facts(measured=(10, 1.0)).packages,
        resolved={"measured": "measured", "unmeasured": "unmeasured"},
    )

    # A budget that fits exactly one of them: the measured one has to win it.
    plans = GreedyPlanner(alpha=0.005).plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=10))
    assert plans[0].imports == ("measured",)


def test_the_reported_saving_is_undiscounted_so_it_can_be_checked():
    # What this checkpoint saves if a request lands on it -- a number an
    # operator can compare against a measured cold start.
    corpus = Corpus.of([req("a")] * 10)
    metadata = facts(a=(10, 2.0))

    plans = GreedyPlanner(alpha=0.005).plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=50))
    assert plans[0].seconds_saved == pytest.approx(20.0)
    assert plans[0].requests_served == 10


def test_alpha_is_the_size_vs_time_threshold():
    # The very first facility is always accepted unconditionally -- there is
    # no baseline cost yet to weigh it against -- so alpha only has something
    # to reject starting with the second pick. anchor is overwhelmingly
    # valuable and always wins that first pick regardless of alpha, which is
    # what gives the borderline package (a 10 MB facility saving 0.05s per
    # request, twice) a real baseline to be judged against: a low alpha still
    # finds it worth its own checkpoint slot on top of anchor's, a high alpha
    # does not. This is the knob the router shares to select the checkpoint
    # that minimises the same alpha*size + residual cost.
    corpus = Corpus.of([req("anchor")] * 1000 + [req("borderline")] * 2)
    metadata = facts(anchor=(1, 100.0), borderline=(10, 0.05))
    budget = Budget(max_checkpoints=10, size_mb=50)

    lenient = GreedyPlanner(alpha=0.005).plan(corpus, metadata, budget)
    strict = GreedyPlanner(alpha=0.1).plan(corpus, metadata, budget)

    assert {p.imports for p in lenient} == {("anchor",), ("borderline",)}
    assert [p.imports for p in strict] == [("anchor",)], (
        "the stricter weight must reject borderline but keep anchor"
    )


def test_a_selected_checkpoint_is_sized_and_charged_for_its_dependencies_too():
    # A package's measured import_time assumes its dependencies are already
    # resident; a checkpoint that omits numpy cannot actually deliver pandas
    # at that cost, so the closure -- not just the requested top-level name --
    # is what gets selected, sized, and charged against the budget.
    corpus = Corpus.of([req("pandas")] * 50)
    metadata = Metadata(
        {
            "pandas": PackageFacts(
                base_import="pandas",
                disk_size_mb=40,
                memory_size_mb=40,
                import_time=0.3,
                loaded_modules=frozenset({"pandas", "numpy"}),
            ),
            "numpy": PackageFacts(
                base_import="numpy",
                disk_size_mb=20,
                memory_size_mb=20,
                import_time=0.1,
                loaded_modules=frozenset({"numpy"}),
            ),
        }
    )

    plans = GreedyPlanner(alpha=0.005).plan(
        corpus, metadata, Budget(max_checkpoints=1, size_mb=500)
    )

    assert plans[0].size_mb == pytest.approx(60)
    assert plans[0].seconds_saved == pytest.approx((0.3 + 0.1) * 50)


def test_reported_imports_are_only_what_a_request_actually_asked_for():
    # The executor is told to import plan.imports, and Python's own import
    # machinery pulls numpy in as a side effect of `import pandas` regardless
    # of whether it is separately named -- so a dependency nothing directly
    # requests does not belong in the reported/executor-facing list, even
    # though (see the test above) it is still sized and charged for.
    corpus = Corpus.of([req("pandas")] * 50)
    metadata = Metadata(
        {
            "pandas": PackageFacts(
                base_import="pandas",
                disk_size_mb=40,
                import_time=0.3,
                loaded_modules=frozenset({"pandas", "numpy"}),
            ),
            "numpy": PackageFacts(
                base_import="numpy",
                disk_size_mb=20,
                import_time=0.1,
                loaded_modules=frozenset({"numpy"}),
            ),
        }
    )

    plans = GreedyPlanner(alpha=0.005).plan(
        corpus, metadata, Budget(max_checkpoints=1, size_mb=500)
    )

    assert plans[0].imports == ("pandas",)


def test_unrelated_requests_each_get_their_own_checkpoint_priced_independently():
    # pandas and scipy are unrelated top-level asks that happen to share a
    # dependency. Checkpoints are never merged, so each becomes its own
    # checkpoint, and each is charged for numpy on its own account rather than
    # splitting or deduplicating it across the two.
    corpus = Corpus.of([req("pandas")] * 30 + [req("scipy")] * 30)
    metadata = Metadata(
        {
            "pandas": PackageFacts(
                base_import="pandas",
                disk_size_mb=40,
                memory_size_mb=40,
                import_time=0.3,
                loaded_modules=frozenset({"pandas", "numpy"}),
            ),
            "scipy": PackageFacts(
                base_import="scipy",
                disk_size_mb=50,
                memory_size_mb=50,
                import_time=0.5,
                loaded_modules=frozenset({"scipy", "numpy"}),
            ),
            "numpy": PackageFacts(
                base_import="numpy",
                disk_size_mb=20,
                memory_size_mb=20,
                import_time=0.1,
                loaded_modules=frozenset({"numpy"}),
            ),
        }
    )

    plans = GreedyPlanner(alpha=0.005).plan(
        corpus, metadata, Budget(max_checkpoints=10, size_mb=500)
    )
    by_imports = {p.imports: p for p in plans}

    # Each request restores from exactly one checkpoint, so a pandas request's
    # numpy overlap is credited only to the pandas checkpoint (its cheapest
    # option) even though the scipy checkpoint also holds numpy.
    assert set(by_imports) == {("pandas",), ("scipy",)}
    assert by_imports[("pandas",)].size_mb == pytest.approx(40 + 20)
    assert by_imports[("pandas",)].seconds_saved == pytest.approx((0.3 + 0.1) * 30)
    assert by_imports[("scipy",)].size_mb == pytest.approx(50 + 20)
    assert by_imports[("scipy",)].seconds_saved == pytest.approx((0.5 + 0.1) * 30)


def test_imports_are_sorted_so_a_plan_diffs_cleanly():
    corpus = Corpus.of([req("zeta", "alpha")] * 50)
    metadata = facts(zeta=(10, 1.0), alpha=(10, 1.0))

    plans = GreedyPlanner(alpha=0.005).plan(corpus, metadata, Budget(max_checkpoints=1, size_mb=50))
    assert list(plans[0].imports) == sorted(plans[0].imports)


# ---------------------------------------------------------------------------
# Against the committed corpus
# ---------------------------------------------------------------------------
def real_inputs() -> tuple[Corpus, Metadata]:
    return Corpus.load(DATA / "datasets" / "cpu.json"), Metadata.load(
        DATA / "metadata" / "cpu.json"
    )


def test_it_beats_the_fixed_plan_on_the_real_corpus():
    # The bar the greedy planner has to clear: the hardcoded pandas+numpy set.
    # Checkpoints are no longer merged, so a fair comparison needs enough
    # slots for greedy's several small, targeted checkpoints to add up against
    # fixed's one broad one.
    corpus, metadata = real_inputs()
    budget = Budget(max_checkpoints=8, size_mb=400)

    greedy = GreedyPlanner(alpha=0.005).plan(corpus, metadata, budget)
    fixed = FixedPlanner().plan(corpus, metadata, budget)[0]

    assert sum(p.requests_served for p in greedy) > fixed.requests_served
    assert sum(p.seconds_saved for p in greedy) > fixed.seconds_saved


def test_every_planned_checkpoint_stays_within_its_budget():
    corpus, metadata = real_inputs()
    budget = Budget(max_checkpoints=4, size_mb=300)

    for plan in GreedyPlanner(alpha=0.005).plan(corpus, metadata, budget):
        assert plan.size_mb <= budget.size_mb


def test_the_plan_round_trips_through_json():
    # Through the JSON form, not back to the object: to_json rounds the scores
    # so a regenerated plan diffs cleanly, and that rounding is deliberate.
    corpus, metadata = real_inputs()
    plans = GreedyPlanner(alpha=0.005).plan(
        corpus, metadata, Budget(max_checkpoints=2, size_mb=400)
    )

    encoded = [p.to_json() for p in plans]
    assert [CheckpointPlan.from_json(e).to_json() for e in encoded] == encoded
