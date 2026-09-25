"""Aggregating results into a report.

Rolls the per-task rows up by domain and by source -- success rate and checkpoint/
cold-start timing percentiles -- and lists the most common imports, then writes both
a raw CSV (one row per task) and a Markdown summary.
"""

from __future__ import annotations

import csv
import math
import statistics
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from readie_evals.results import ResultsStore

_SUCCESS = {"ok", "repaired_ok"}


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * fraction
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - rank) + ordered[high] * (rank - low)


def _floats(rows: list[dict[str, object]], key: str) -> list[float]:
    return [float(cast("float", row[key])) for row in rows if row.get(key) is not None]


def _group_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    successes = [row for row in rows if row["status"] in _SUCCESS]
    checkpoint = _floats(successes, "checkpoint_seconds")
    cold_start = _floats(successes, "cold_start_seconds")
    checkpoint_median = statistics.median(checkpoint) if checkpoint else None
    cold_start_median = statistics.median(cold_start) if cold_start else None
    return {
        "tasks": len(rows),
        "success": len(successes),
        "success_rate": (len(successes) / len(rows)) if rows else 0.0,
        "checkpoint_median": checkpoint_median,
        "checkpoint_p95": _percentile(checkpoint, 0.95),
        "cold_start_median": cold_start_median,
        "cold_start_p95": _percentile(cold_start, 0.95),
        "speedup": _speedup(cold_start_median, checkpoint_median),
    }


def _speedup(cold_start: float | None, checkpoint: float | None) -> float | None:
    """How many times faster the checkpoint path is than a cold start."""
    if cold_start is None or not checkpoint:
        return None
    return cold_start / checkpoint


def _by(rows: list[dict[str, object]], key: str) -> dict[str, dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        groups.setdefault(str(row[key]), []).append(row)
    return {name: _group_summary(group) for name, group in sorted(groups.items())}


def _import_counts(rows: list[dict[str, object]]) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for row in rows:
        imports = row.get("imports") or []
        if isinstance(imports, list):
            counter.update(str(name) for name in imports)
    return counter.most_common(20)


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _table(title: str, summary: dict[str, dict[str, object]]) -> list[str]:
    header = f"| {title} | tasks | success | rate | checkpoint p50 | cold-start p50 | speedup |"
    lines = [f"### By {title}", "", header, "|---|---|---|---|---|---|---|"]
    for name, stats in summary.items():
        rate = f"{cast('float', stats['success_rate']) * 100:.0f}%"
        speedup = stats["speedup"]
        speedup_text = "-" if speedup is None else f"{cast('float', speedup):.1f}x"
        lines.append(
            f"| {name} | {stats['tasks']} | {stats['success']} | {rate} "
            f"| {_fmt(stats['checkpoint_median'])} | {_fmt(stats['cold_start_median'])} "
            f"| {speedup_text} |",
        )
    lines.append("")
    return lines


def build_markdown(rows: list[dict[str, object]], run_id: str) -> str:
    """Render the summary Markdown for a run's rows."""
    total = len(rows)
    ok = sum(1 for row in rows if row["status"] in _SUCCESS)
    lines = [
        f"# Readie eval report — run `{run_id}`",
        "",
        f"- Tasks: **{total}**",
        f"- Succeeded: **{ok}** ({(ok / total * 100) if total else 0:.0f}%)",
        f"- Failed: **{total - ok}**",
        "",
        *_table("category", _by(rows, "category")),
        *_table("source", _by(rows, "source")),
        "### Most common imports",
        "",
        "| import | tasks |",
        "|---|---|",
    ]
    lines += [f"| {name} | {count} |" for name, count in _import_counts(rows)]
    lines.append("")
    return "\n".join(lines)


def write_reports(store: ResultsStore, run_id: str, reports_dir: Path) -> tuple[Path, Path]:
    """Write the CSV and Markdown reports for a run, returning their paths."""
    rows = list(store.rows(run_id))
    reports_dir.mkdir(parents=True, exist_ok=True)
    csv_path = reports_dir / f"results-{run_id}.csv"
    md_path = reports_dir / f"report-{run_id}.md"

    if rows:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    md_path.write_text(build_markdown(rows, run_id))
    return csv_path, md_path
