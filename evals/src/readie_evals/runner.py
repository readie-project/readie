"""Running a corpus and recording what happened.

Resumable: a task that already succeeded on any past run is skipped, so a
paused run continues on restart and re-invoking against a corpus that has
grown (``sample`` keeps appending) only measures what isn't already known
good. A task whose latest attempt failed is never skipped this way -- it is
retried on every subsequent run until it either succeeds or the corpus is
otherwise changed, which is what makes "did the fix work" a matter of just
running again. Repair itself
happens inside the executor, in-session, one attempt per failing cell per side
(see ``execution.py``); this layer just supplies the task-level context
(category) a repair attempt needs and records whatever comes back -- ``ok``,
``repaired_ok`` (checkpoint and/or cold-start needed an in-place fix), or
``failed``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from readie_evals.execution import ExecOutcome, Executor, RepairFn, Target, TaskExecutionError
from readie_evals.models import Task
from readie_evals.results import Result, ResultsStore
from readie_evals.taxonomy import GENERATED_SOURCES

#: Given a task's category, a failing cell's own code, and the error it raised,
#: return the fixed code and any additional packages it needs, or ``None`` to
#: give up. Wired to the agent, or absent. Category-aware (unlike execution.py's
#: narrower ``RepairFn``, which this gets bound down to per task in ``_execute``).
Repairer = Callable[[str, str, str, str], "tuple[str, tuple[str, ...]] | None"]


@dataclass(slots=True)
class RunStats:
    """A tally of what a run did."""

    ok: int = 0
    repaired: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def attempted(self) -> int:
        """How many tasks were run (not skipped)."""
        return self.ok + self.repaired + self.failed


class Runner:
    """Executes tasks and records every outcome.

    ``concurrency`` bounds how many *tasks* are dispatched at once, on a pool
    of this Runner's own -- separate from whatever pool the ``executor`` uses
    to bound concurrent containers (see ``Executor``'s docstring for why they
    must stay separate pools). 1 (the default) keeps the original strictly
    sequential behaviour.
    """

    def __init__(
        self,
        executor: Executor,
        results: ResultsStore,
        *,
        target: Target,
        repair: Repairer | None = None,
        on_line: Callable[[str], None] = print,
        concurrency: int = 1,
    ) -> None:
        """Wire the executor, the results store and (optionally) the repairer."""
        self._executor = executor
        self._results = results
        self._target = target
        self._repair = repair
        self._on_line = on_line
        self._concurrency = concurrency
        self._stats_lock = threading.Lock()

    def run(self, corpus: list[Task], *, run_id: str, limit: int | None = None) -> RunStats:
        """Run every not-yet-recorded task, returning a tally."""
        stats = RunStats()
        dispatched = 0

        def eligible(task: Task) -> bool:
            nonlocal dispatched
            if self._target is Target.LOCAL and task.source not in GENERATED_SOURCES:
                # The local target runs code in this process without a sandbox, so
                # it is only ever pointed at trusted, self-generated tasks.
                stats.skipped += 1
                return False
            if self._results.should_skip(task.id):
                stats.skipped += 1
                return False
            return True

        if self._concurrency <= 1:
            for task in corpus:
                if limit is not None and dispatched >= limit:
                    break
                if not eligible(task):
                    continue
                dispatched += 1
                self._run_one(task, run_id, stats)
            return stats

        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            futures = []
            for task in corpus:
                if limit is not None and dispatched >= limit:
                    break
                if not eligible(task):
                    continue
                dispatched += 1
                futures.append(pool.submit(self._run_one, task, run_id, stats))
            for future in futures:
                future.result()
        return stats

    def _run_one(self, task: Task, run_id: str, stats: RunStats) -> None:
        try:
            outcome = self._execute(task)
        except TaskExecutionError as failure:
            self._results.record(_failure(task, run_id, failure))
            with self._stats_lock:
                stats.failed += 1
            self._on_line(f"    failed   {_label(task)}: {failure.error_type}")
            return
        self._results.record(_success(task, run_id, outcome))
        with self._stats_lock:
            if outcome.repaired:
                stats.repaired += 1
            else:
                stats.ok += 1
        label = "repaired" if outcome.repaired else "ok"
        self._on_line(f"    {label:<8} {_label(task)}: {_timing(outcome)}")

    def _execute(self, task: Task) -> ExecOutcome:
        cell_repair: RepairFn | None = None
        if self._repair is not None:
            repair, category = self._repair, task.category

            def cell_repair(
                code: str, error_type: str, error_message: str
            ) -> tuple[str, tuple[str, ...]] | None:
                return repair(category, code, error_type, error_message)

        return self._executor.run(
            code=task.code,
            memory=task.memory,
            packages=task.packages,
            timeout=task.timeout,
            repair=cell_repair,
        )


def _label(task: Task) -> str:
    if task.is_session:
        return f"{task.source}/{task.category} ({task.session_kind}, {len(task.code)} cells)"
    return f"{task.source}/{task.category}"


def _success(task: Task, run_id: str, outcome: ExecOutcome) -> Result:
    return Result(
        task_id=task.id,
        run_id=run_id,
        source=task.source,
        category=task.category,
        status="repaired_ok" if outcome.repaired else "ok",
        imports=task.imports,
        packages=task.packages,
        checkpoint_seconds=outcome.checkpoint_seconds,
        cold_start_seconds=outcome.cold_start_seconds,
        repaired=outcome.repaired,
        payload_bytes=outcome.payload_bytes,
        value_repr=outcome.value_repr,
        session_kind=task.session_kind,
        cells=len(task.code),
        slug=task.slug,
        provenance=task.provenance,
    )


def _failure(task: Task, run_id: str, failure: TaskExecutionError) -> Result:
    return Result(
        task_id=task.id,
        run_id=run_id,
        source=task.source,
        category=task.category,
        status="failed",
        imports=task.imports,
        packages=task.packages,
        error_type=failure.error_type,
        error_message=failure.message,
        session_kind=task.session_kind,
        cells=len(task.code),
        slug=task.slug,
        provenance=task.provenance,
    )


def _timing(outcome: ExecOutcome) -> str:
    parts = []
    if outcome.checkpoint_seconds is not None:
        parts.append(f"checkpoint {outcome.checkpoint_seconds:.3f}s")
    if outcome.cold_start_seconds is not None:
        parts.append(f"cold-start {outcome.cold_start_seconds:.3f}s")
    return ", ".join(parts)
