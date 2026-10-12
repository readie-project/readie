"""Run the items of a corpus-format file through the evaluation server.

One item at a time: a request is one data point, and the server runs its two
lanes (optimized start and cold start) at once. Every repetition is a line of
JSON in the results file, written as soon as it arrives. A run starts the file
over, so it holds that run's results only; ``resume`` continues the file instead.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from readie_pipeline.corpus.clean import code_key
from readie_pipeline.corpus.models import CorpusItem
from readie_pipeline.evals.client import EvalError, EvalServer, RunOptions

#: Step errors that mean the item ran out of time, not that it failed.
TIMEOUT_ERRORS = frozenset({"RemoteTimeoutError", "OutOfTime"})
#: Readie answers a deadline error with this text when the sandbox died instead of
#: running out of time: the worker lost its connection to the executor, so nothing
#: of the code's own is to blame. Seen when a sandbox restored from a checkpoint
#: crashed while the cold start of the same code finished.
SANDBOX_LOST = "could not complete the execution"
#: Readie answers this when the worker has no free sandbox slot or memory for the
#: request. The code never ran, so the result says nothing about it.
BUSY_ERRORS = frozenset({"ResourceExhaustedError"})
_MAX_ERROR_CHARS = 4000
_MAX_OUTPUT_CHARS = 500

OK = "ok"
FAILED = "failed"
TIMEOUT = "timeout"
LOST = "sandbox_lost"
OPTIMIZED_FAILURE = "sandbox_failure"
ERROR = "error"
BUSY = "busy"


@dataclass(frozen=True, slots=True)
class Selection:
    """Which items of the file to run."""

    sources: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    ids: tuple[str, ...] = ()
    limit: int | None = None


@dataclass(slots=True)
class EvalRun:
    """What a run did."""

    items: int = 0
    runs: int = 0
    skipped_done: int = 0
    skipped_timeouts: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)


def item_id(item: CorpusItem) -> str:
    """A stable id for an item: a hash of its code."""
    return code_key(item.code)[:16]


def select(items: Iterable[CorpusItem], selection: Selection) -> list[CorpusItem]:
    """The items matching the filters, in file order, up to the limit."""
    chosen: list[CorpusItem] = []
    for item in items:
        if selection.sources and item.source.value not in selection.sources:
            continue
        if selection.domains and item.domain not in selection.domains:
            continue
        if selection.ids and not any(item_id(item).startswith(i) for i in selection.ids):
            continue
        chosen.append(item)
        if selection.limit is not None and len(chosen) >= selection.limit:
            break
    return chosen


def is_sandbox_lost(step: dict[str, Any]) -> bool:
    """Whether a step failed because its sandbox died, not because it ran out of time."""
    return step.get("error_type") == "RemoteTimeoutError" and SANDBOX_LOST in str(
        step.get("error", "")
    )


def classify(lanes: dict[str, Any]) -> str:
    """``timeout``, ``sandbox_failure``, ``sandbox_lost``, ``failed`` or ``ok`` for a pair of lanes.

    A real timeout wins. Then a sandbox that died: ``sandbox_failure`` when only
    the optimized start lost its sandbox, which points at the checkpoint restore
    and not at the code, and ``sandbox_lost`` for every other pattern (both starts
    lost one, or only the cold start did). Then an ordinary failure. A lost
    sandbox is kept apart from a timeout because it says nothing about how long
    the code takes.
    """
    failed = [
        step
        for lane in lanes.values()
        for step in lane.get("steps", [])
        if not step.get("ok", False)
    ]
    if any(step.get("error_type") in BUSY_ERRORS for step in failed):
        return BUSY
    if any(
        step.get("error_type") in TIMEOUT_ERRORS and not is_sandbox_lost(step) for step in failed
    ):
        return TIMEOUT
    lost = {name for name, lane in lanes.items() if _lane_lost_a_sandbox(lane)}
    if lost == {"optimized"}:
        return OPTIMIZED_FAILURE
    if lost:
        return LOST
    if any(not lane.get("ok", False) for lane in lanes.values()):
        return FAILED
    return OK


def _lane_lost_a_sandbox(lane: dict[str, Any]) -> bool:
    return any(is_sandbox_lost(step) for step in lane.get("steps", []))


def load_progress(path: Path) -> tuple[set[tuple[str, int]], Counter[str]]:
    """Which (item, repetition) pairs are done and how often each item timed out."""
    done: set[tuple[str, int]] = set()
    timeouts: Counter[str] = Counter()
    if not path.exists():
        return done, timeouts
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        outcome = row.get("outcome")
        if "optimized" in row and "cold" in row:
            outcome = classify({"optimized": row["optimized"], "cold": row["cold"]})
        if outcome == BUSY:
            continue  # the worker was full; a later run tries the item again
        done.add((row["id"], int(row["repetition"])))
        if row.get("outcome") == TIMEOUT:
            timeouts[row["id"]] += 1
    return done, timeouts


def run_evaluation(
    items: Sequence[CorpusItem],
    server: EvalServer,
    options: RunOptions,
    results_path: Path,
    *,
    repetitions: int = 3,
    skip_timeouts_after: int = 1,
    resume: bool = False,
    busy_retries: int = 5,
    busy_wait_s: float = 20.0,
    parallel: int = 1,
    on_line: Callable[[str], None] = print,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> EvalRun:
    """Run every item ``repetitions`` times and append the results to ``results_path``.

    An item that has timed out ``skip_timeouts_after`` times is not repeated, so
    one slow notebook does not cost ``repetitions`` full budgets.

    When the worker has no room (outcome ``busy``) the item is tried again after
    ``busy_wait_s`` seconds, doubling the wait each time, up to ``busy_retries``
    more times. An item still busy after that is recorded as ``busy``, which a
    later run with ``resume`` tries again.

    The results file is emptied first unless ``resume`` is set, in which case the
    runs it already holds are skipped and new ones are appended.

    ``parallel`` items run at the same time. Each item already runs two sandboxes (the
    optimized and the cold start), so the worker needs room for twice that many.
    """
    results_path.parent.mkdir(parents=True, exist_ok=True)
    done, timeouts = load_progress(results_path) if resume else (set(), Counter())
    report = EvalRun(items=len(items))
    started = clock()
    lock = threading.Lock()

    with results_path.open("a" if resume else "w", encoding="utf-8") as sink:

        def run_item(number: int, item: CorpusItem) -> None:
            ident = item_id(item)
            for repetition in range(1, repetitions + 1):
                with lock:
                    if (ident, repetition) in done:
                        report.skipped_done += 1
                        continue
                    if 0 < skip_timeouts_after <= timeouts[ident]:
                        report.skipped_timeouts += 1
                        continue
                row = _run_once(item, ident, repetition, server, options=options, now=now)
                for retry in range(busy_retries):
                    if row["outcome"] != BUSY:
                        break
                    wait = busy_wait_s * 2**retry
                    on_line(f"    the worker is full; trying again in {wait:g} s")
                    sleep(wait)
                    row = _run_once(item, ident, repetition, server, options=options, now=now)
                with lock:
                    sink.write(json.dumps(row) + "\n")
                    sink.flush()
                    report.runs += 1
                    report.outcomes[row["outcome"]] += 1
                    if row["outcome"] == TIMEOUT:
                        timeouts[ident] += 1
                    on_line(_progress(number, len(items), repetition, row, clock() - started))

        # An item's repetitions stay in order inside one job, so skipping an item that
        # timed out still works; only different items overlap.
        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            for future in [pool.submit(run_item, n, item) for n, item in enumerate(items, 1)]:
                future.result()
    return report


def _run_once(
    item: CorpusItem,
    ident: str,
    repetition: int,
    server: EvalServer,
    *,
    options: RunOptions,
    now: Callable[[], datetime],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": ident,
        "ref": item.ref,
        "source": item.source.value,
        "domain": item.domain,
        "steps": len(item.code),
        "repetition": repetition,
        "at": now().isoformat(timespec="seconds"),
        "options": {"memory": options.memory, "timeout_s": options.timeout_s},
    }
    try:
        response = server.run(list(item.code), options)
    except EvalError as exc:
        return {**row, "outcome": ERROR, "error": str(exc)[:_MAX_ERROR_CHARS]}
    lanes = {name: _trim(lane) for name, lane in response["lanes"].items()}
    return {**row, "outcome": classify(lanes), **lanes}


def _trim(lane: dict[str, Any]) -> dict[str, Any]:
    """The lane without the bulk: long tracebacks and output are cut."""
    steps = [
        {
            **step,
            "error": str(step.get("error", ""))[:_MAX_ERROR_CHARS],
            "output": str(step.get("output", ""))[:_MAX_OUTPUT_CHARS],
        }
        for step in lane.get("steps", [])
    ]
    return {**lane, "steps": steps}


def _progress(number: int, total: int, repetition: int, row: dict[str, Any], elapsed: float) -> str:
    timings = ""
    if "optimized" in row and "cold" in row:
        timings = f" optimized {row['optimized']['total_s']}s, cold {row['cold']['total_s']}s"
    return (
        f"[{number}/{total}] {row['source']} {row['id']} run {repetition}: "
        f"{row['outcome']}{timings} ({elapsed / 60:.1f} min elapsed)"
    )
