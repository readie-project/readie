"""Summarise a results file: optimized start against cold start.

Timings come from the server's ``duration_s`` and are only compared for items whose
runs finished cleanly in both lanes, since a run that failed or timed out says
nothing about how fast it started. Failures and timeouts are counted, not hidden.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Upper bounds of the buckets items are grouped into by their cold duration.
_BUCKETS = ((5.0, "under 5 s"), (30.0, "5 to 30 s"), (120.0, "30 to 120 s"))
_LAST_BUCKET = "over 120 s"


@dataclass(frozen=True, slots=True)
class ItemSummary:
    """The repetitions of one item, reduced to medians."""

    id: str
    source: str
    domain: str
    steps: int
    repetitions: int
    optimized_s: float
    cold_s: float
    optimized_first_s: float
    cold_first_s: float

    @property
    def saved_s(self) -> float:
        """Seconds the optimized start saved over the cold start."""
        return self.cold_s - self.optimized_s

    @property
    def speedup(self) -> float:
        """How many times faster the optimized start was."""
        return self.cold_s / self.optimized_s if self.optimized_s > 0 else 0.0


def load_results(path: Path) -> list[dict[str, Any]]:
    """The rows of a results file, skipping lines that are not JSON.

    A row's outcome is worked out again from its lanes, so a file written under an
    older rule (before ``sandbox_failure`` existed, say) reports the same way as a
    new one. Rows without lanes (a server error) keep the outcome they were given.
    """
    from readie_pipeline.evals.run import classify  # noqa: PLC0415 - avoids a cycle

    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if "optimized" in row and "cold" in row:
            row["outcome"] = classify({"optimized": row["optimized"], "cold": row["cold"]})
        rows.append(row)
    return rows


def summarise_items(rows: Iterable[dict[str, Any]]) -> list[ItemSummary]:
    """One summary per item that finished cleanly in both lanes at least once."""
    by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("outcome") == "ok":
            by_item[row["id"]].append(row)

    summaries: list[ItemSummary] = []
    for ident, runs in by_item.items():
        first = runs[0]
        summaries.append(
            ItemSummary(
                id=ident,
                source=first["source"],
                domain=first["domain"],
                steps=int(first["steps"]),
                repetitions=len(runs),
                optimized_s=statistics.median(r["optimized"]["total_s"] for r in runs),
                cold_s=statistics.median(r["cold"]["total_s"] for r in runs),
                optimized_first_s=statistics.median(_first(r["optimized"]) for r in runs),
                cold_first_s=statistics.median(_first(r["cold"]) for r in runs),
            )
        )
    return summaries


def _first(lane: dict[str, Any]) -> float:
    steps = lane.get("steps") or [{"duration_s": 0.0}]
    return float(steps[0]["duration_s"])


def bucket(cold_s: float) -> str:
    """The duration bucket an item falls in."""
    for bound, label in _BUCKETS:
        if cold_s < bound:
            return label
    return _LAST_BUCKET


def describe(items: list[ItemSummary]) -> str:
    """One line of statistics for a group of items."""
    if not items:
        return "no items"
    saved = [i.saved_s for i in items]
    ratios = [i.speedup for i in items if i.speedup > 0]
    faster = sum(1 for s in saved if s > 0)
    return (
        f"{len(items):5} items | optimized {statistics.median(i.optimized_s for i in items):7.2f} s"
        f" | cold {statistics.median(i.cold_s for i in items):7.2f} s"
        f" | saved {statistics.median(saved):6.2f} s"
        f" | speedup {statistics.median(ratios) if ratios else 0:5.2f}x"
        f" | optimized faster in {100 * faster / len(items):3.0f}%"
    )


def format_report(rows: list[dict[str, Any]]) -> str:
    """The text of the report."""
    lines: list[str] = []
    outcomes = Counter(row.get("outcome", "?") for row in rows)
    items = summarise_items(rows)
    lines.append(f"{len(rows)} runs over {len({r['id'] for r in rows})} items")
    lines.append("outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items())))
    lines.append("")
    lines.append("durations are medians of the server's duration_s over the repetitions")
    lines.append("")
    lines.append("all items that ran cleanly in both lanes")
    lines.append("  " + describe(items))
    multi = [i for i in items if i.steps > 1]
    if multi:
        first = [
            ItemSummary(i.id, i.source, i.domain, i.steps, i.repetitions,
                        i.optimized_first_s, i.cold_first_s, 0, 0)
            for i in multi
        ]  # fmt: skip
        lines.append("first step of the multi-step items (where the start happens)")
        lines.append("  " + describe(first))

    for title, key in (("source", lambda i: i.source), ("domain", lambda i: i.domain)):
        lines += ["", f"by {title}", *_groups(items, key)]
    lines += ["", "by cold duration", *_groups(items, lambda i: bucket(i.cold_s))]
    lines += ["", *_problems(rows)]
    return "\n".join(lines).rstrip() + "\n"


def _groups(items: list[ItemSummary], key: Callable[[ItemSummary], str]) -> list[str]:
    groups: dict[str, list[ItemSummary]] = defaultdict(list)
    for item in items:
        groups[key(item)].append(item)
    return [f"  {name:28} {describe(group)}" for name, group in sorted(groups.items())]


def _lost_by_lane(rows: list[dict[str, Any]], outcome: str) -> dict[str, int]:
    """How many runs of ``outcome`` lost a sandbox in each lane, and over how many items."""
    from readie_pipeline.evals.run import is_sandbox_lost  # noqa: PLC0415 - avoids a cycle

    counts = {"optimized": 0, "cold": 0}
    items: set[str] = set()
    for row in rows:
        if row.get("outcome") != outcome:
            continue
        items.add(row["id"])
        for lane in counts:
            if any(is_sandbox_lost(s) for s in row.get(lane, {}).get("steps", [])):
                counts[lane] += 1
    return {**counts, "items": len(items)}


def _problems(rows: list[dict[str, Any]]) -> list[str]:
    timeouts = {r["id"] for r in rows if r.get("outcome") == "timeout"}
    busy = sum(1 for r in rows if r.get("outcome") == "busy")
    from readie_pipeline.evals.run import LOST, OPTIMIZED_FAILURE  # noqa: PLC0415 - avoids a cycle

    failure = _lost_by_lane(rows, OPTIMIZED_FAILURE)
    lost = _lost_by_lane(rows, LOST)
    failures: Counter[str] = Counter()
    for row in rows:
        if row.get("outcome") == "failed":
            for lane in ("optimized", "cold"):
                for step in row.get(lane, {}).get("steps", []):
                    if not step.get("ok", True):
                        failures[f"{lane}: {step.get('error_type', '?')}"] += 1
        elif row.get("outcome") == "error":
            failures["server or network: " + str(row.get("error", ""))[:60]] += 1
    out = [f"timeouts: {len(timeouts)} items ran out of time and are left out of the comparison"]
    if busy:
        out.append(
            f"busy: {busy} runs never started because the worker was full; rerun to retry them"
        )
    out.append(
        "sandbox failure (only the optimized start lost its sandbox while the cold start "
        f"did not; left out of the comparison): {failure['items']} items, "
        f"{failure['optimized']} runs"
    )
    out.append(
        "sandbox lost (any other pattern; left out of the comparison): "
        f"{lost['items']} items, optimized lane {lost['optimized']} runs, "
        f"cold lane {lost['cold']} runs"
    )
    out.append("failed steps by kind:")
    out.extend(f"  {count:5} {kind}" for kind, count in failures.most_common(10))
    if not failures:
        out.append("  none")
    return out
