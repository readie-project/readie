"""Choose what to pre-import, by greedy set cover.

# The problem

A checkpoint is a process image with some packages already imported. Restoring
it saves whatever those imports would have cost, for every request that uses
them. It costs disk and memory proportional to what it holds, and a worker can
only keep so many.

So: choose ``K`` package sets, each under a size budget, maximising the import
time saved across the corpus. That is a weighted maximum coverage problem, which
is NP-hard, and greedy is the standard answer — it is within ``1 - 1/e`` of
optimal for a single monotone submodular objective, and the marginal-gain
ordering is exactly the intuition you would apply by hand.

# The objective

For one request ``r`` and a candidate set ``S``::

    value(r, S) = sum of import_time[p] for p in imports(r) & S

Summed over the corpus, and divided by marginal size to get gain per megabyte.
Import time rather than request count, because saving 4 seconds of `torch` for
100 requests beats saving 3 milliseconds of `json` for 5,000.

# Why several checkpoints

**A request restores exactly one checkpoint.** It gets what that checkpoint
holds and nothing from any other. So the value of a *set* of checkpoints is not
their sum — it is, for each request, the best single one available to it::

    total(plans) = sum over r of (max over plans of value(r, plan))

which makes the marginal value of a new checkpoint the *improvement* it offers
over what each request can already reach::

    gain(new) = sum over r of max(0, value(r, new) - best_so_far[r])

That single change is what makes the checkpoints specialise. Once a
pandas/numpy/sklearn checkpoint exists, another one like it gains nothing,
because every request it would serve is already served as well. A torch
checkpoint gains a great deal. Ranking by absolute value instead — the obvious
first attempt — produces K identical copies of the most popular set, since the
same packages win every round.

The same "improvement over what is already reachable" rule applies *within* a
checkpoint, when deciding which package to add next.

# Determinism

No randomness, and every tie breaks on the package name. The same corpus and
metadata always produce a byte-identical plan, which is what lets a plan be
reviewed in a diff and pinned by a golden test.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass

from crfs_pipeline.corpus.models import Corpus, Request
from crfs_pipeline.metadata.models import Metadata
from crfs_pipeline.planning.ports import Budget, CheckpointPlan

#: Standard library modules are never worth a checkpoint slot. They install
#: nothing, several are already imported before user code runs, and the rest
#: cost microseconds -- but with no measured import time they fall back to the
#: default below, which multiplied across thousands of requests outranks the
#: packages that actually matter. `json` and `collections` won slots this way.
STDLIB = frozenset(sys.stdlib_module_names)

#: A package used by fewer than this fraction of requests is not worth a
#: checkpoint slot, however cheap it is: the tail of an 88-package corpus is
#: mostly packages used once, and including them crowds out the ones that matter.
MIN_COVERAGE = 0.002

#: Packages with no measured import time still cost disk. Charging them a
#: nominal saving keeps a package the analysis could not profile from being
#: treated as literally worthless -- but small enough that a measured package
#: always wins.
UNMEASURED_IMPORT_TIME = 0.01

#: Every package occupies at least this much of the budget, so a package
#: reported as 0 MB cannot look free and be added without limit.
MIN_SIZE_MB = 0.5

#: The size-vs-time weight, in seconds per megabyte. It is the single knob that
#: trades a checkpoint's disk footprint against the import time it saves, and it
#: is **shared with the router**: the planner adds a package while its marginal
#: saving exceeds ``alpha * size_mb`` (i.e. while doing so lowers
#: ``alpha*size + residual_load``), and the router selects the checkpoint that
#: minimises exactly that cost. The two must use the same value; the default is
#: mirrored by the router's ``Settings.alpha`` and a comment there points back.
DEFAULT_ALPHA = 0.002


@dataclass(slots=True)
class _Candidate:
    """A package the planner may add, with its costs precomputed."""

    name: str
    import_time: float
    size_mb: float
    users: tuple[int, ...]
    """Indices of the requests that import it. Precomputed because the inner
    loop asks this for every package on every round."""


class GreedyPlanner:
    """Selects checkpoint contents by marginal saving per megabyte."""

    def __init__(
        self,
        *,
        min_coverage: float = MIN_COVERAGE,
        alpha: float = DEFAULT_ALPHA,
    ) -> None:
        self._min_coverage = min_coverage
        self._alpha = alpha

    def plan(self, corpus: Corpus, metadata: Metadata, budget: Budget) -> Sequence[CheckpointPlan]:
        """Choose up to ``budget.max_checkpoints`` sets, best first."""
        requests = corpus.requests
        if not requests:
            return []

        candidates = self._candidates(requests, metadata)
        if not candidates:
            return []

        # best[i] is the import time request i can already save by restoring the
        # best checkpoint planned so far. Every round maximises improvement over
        # this, which is what stops the rounds from producing the same set.
        best = [0.0] * len(requests)

        plans: list[CheckpointPlan] = []
        for _ in range(budget.max_checkpoints):
            chosen = self._one_checkpoint(candidates, best, budget)
            if not chosen:
                break  # no remaining set improves on what is already planned

            plans.append(self._describe(chosen, requests, metadata))
            self._absorb(chosen, best)

        return plans

    # -- internals ---------------------------------------------------------
    def _candidates(self, requests: Sequence[Request], metadata: Metadata) -> list[_Candidate]:
        """Packages worth considering, ordered for a deterministic tiebreak."""
        users: dict[str, list[int]] = {}
        for index, request in enumerate(requests):
            for name in request.top_level_imports:
                users.setdefault(name, []).append(index)

        floor = max(1, int(len(requests) * self._min_coverage))

        return sorted(
            (
                _Candidate(
                    name=name,
                    import_time=metadata.import_time(name) or UNMEASURED_IMPORT_TIME,
                    size_mb=max(metadata.size_mb(name), MIN_SIZE_MB),
                    users=tuple(indices),
                )
                for name, indices in users.items()
                if len(indices) >= floor and name not in STDLIB
            ),
            key=lambda c: c.name,
        )

    def _one_checkpoint(
        self,
        candidates: Sequence[_Candidate],
        best: Sequence[float],
        budget: Budget,
    ) -> list[_Candidate]:
        """Fill one checkpoint, adding whichever package improves it most."""
        chosen: list[_Candidate] = []
        chosen_names: set[str] = set()
        spent = 0.0

        # current[i] is what this checkpoint saves request i so far. Kept as a
        # running total rather than recomputed, because the inner loop over
        # candidates already costs one pass over the corpus.
        current: dict[int, float] = {}

        while True:
            winner: _Candidate | None = None
            # A package is worth a slot only while its saving per MB clears
            # alpha; equivalently, while adding it lowers alpha*size + residual.
            winning_rate = self._alpha

            for candidate in candidates:
                if candidate.name in chosen_names:
                    continue
                if spent + candidate.size_mb > budget.size_mb:
                    continue

                rate = self._gain(candidate, current, best) / candidate.size_mb
                # Strictly greater, so the name-sorted candidate order breaks
                # ties and the plan is reproducible.
                if rate > winning_rate:
                    winner, winning_rate = candidate, rate

            if winner is None:
                return chosen

            chosen.append(winner)
            chosen_names.add(winner.name)
            spent += winner.size_mb
            for index in winner.users:
                current[index] = current.get(index, 0.0) + winner.import_time

    def _gain(
        self,
        candidate: _Candidate,
        current: dict[int, float],
        best: Sequence[float],
    ) -> float:
        """How much adding this package improves on what requests already have.

        Only the part above ``best`` counts. A package that saves four seconds
        for a request already saving five elsewhere contributes nothing, because
        that request would restore the other checkpoint.
        """
        total = 0.0
        for index in candidate.users:
            have = current.get(index, 0.0)
            already = best[index]
            before = max(0.0, have - already)
            after = max(0.0, have + candidate.import_time - already)
            total += after - before
        return total

    def _describe(
        self,
        chosen: Sequence[_Candidate],
        requests: Sequence[Request],
        metadata: Metadata,
    ) -> CheckpointPlan:
        """Score a finished checkpoint against the corpus.

        Reported undiscounted -- what this checkpoint saves if a request lands
        on it -- because that is the number an operator can check against a
        measured cold start.
        """
        names = frozenset(c.name for c in chosen)

        served = 0
        saved = 0.0
        for request in requests:
            overlap = names & request.top_level_imports
            if overlap:
                served += 1
                saved += sum(metadata.import_time(n) for n in overlap)

        return CheckpointPlan(
            # Sorted so the plan diffs cleanly; the greedy order is recorded in
            # the scores, not in the sequence.
            imports=tuple(sorted(names)),
            requests_served=served,
            seconds_saved=saved,
            size_mb=sum(c.size_mb for c in chosen),
        )

    def _absorb(self, chosen: Sequence[_Candidate], best: list[float]) -> None:
        """Record what this checkpoint now offers each request.

        A later checkpoint is then judged only on what it adds beyond this,
        which is what makes the plan specialise instead of repeating itself.
        """
        for candidate in chosen:
            for index in candidate.users:
                best[index] = max(best[index], 0.0)

        totals: dict[int, float] = {}
        for candidate in chosen:
            for index in candidate.users:
                totals[index] = totals.get(index, 0.0) + candidate.import_time

        for index, saving in totals.items():
            best[index] = max(best[index], saving)
