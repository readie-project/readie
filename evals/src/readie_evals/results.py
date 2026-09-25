"""The results store: one measured run of a task, in JSON.

Records are appended as JSON Lines to a single file -- one complete JSON object per
line, so an interrupted run leaves a readable file and the next run continues.
Keyed by ``(task_id, run_id)`` on disk, but the runner's own resume check
(``should_skip``) ignores ``run_id`` and asks only "has this task ever *succeeded*,
under any run": a corpus grows over time (``sample`` keeps appending), so pinning
"already done" to one run_id would re-run everything already measured under an
earlier run_id the moment the corpus changes and the default run_id (a hash of every
task id) shifts with it. A failed task is deliberately not "done" -- it stays
eligible for every later run, since re-attempting it (after a fix, or just because
infra flaked) is the entire point of measuring again. ``run_id`` still tags every
record -- it is which invocation produced a measurement, for provenance -- it just no
longer gates whether one is taken. Re-recording a key appends a new line that wins on
read (last-write-wins), so a repaired result replaces an earlier failure without
rewriting the file.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: Statuses that count as "this task is done" for resume purposes. A failed task is
#: excluded on purpose: see the module docstring.
_SETTLED_STATUSES = frozenset({"ok", "repaired_ok"})


@dataclass(frozen=True, slots=True)
class Result:
    """One task's outcome under one run.

    ``checkpoint_seconds`` is the optimized path (checkpoint restore) and
    ``cold_start_seconds`` the same call with the optimization disabled.
    """

    task_id: str
    run_id: str
    source: str
    category: str
    status: str
    imports: tuple[str, ...]
    packages: tuple[str, ...]
    checkpoint_seconds: float | None = None
    cold_start_seconds: float | None = None
    error_type: str | None = None
    error_message: str | None = None
    repaired: bool = False
    payload_bytes: int | None = None
    value_repr: str | None = None
    session_kind: str | None = None
    """``None`` for a standalone task, else how its cells were produced."""
    cells: int = 1
    """How many code entries the task ran (1 for a standalone task)."""
    slug: str = ""
    """The platform's own identifier ("user/kernel-slug", "org/model-name"), or ""."""
    provenance: str = ""
    """Where the task's code came from: a Kaggle/HuggingFace URL, or "" (generated)."""

    def to_json(self) -> dict[str, object]:
        """Render to the on-disk shape, stamping the write time."""
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "source": self.source,
            "category": self.category,
            "status": self.status,
            "checkpoint_seconds": self.checkpoint_seconds,
            "cold_start_seconds": self.cold_start_seconds,
            "imports": list(self.imports),
            "packages": list(self.packages),
            "error_type": self.error_type,
            "error_message": self.error_message,
            "repaired": self.repaired,
            "payload_bytes": self.payload_bytes,
            "value_repr": self.value_repr,
            "session_kind": self.session_kind,
            "cells": self.cells,
            "slug": self.slug,
            "provenance": self.provenance,
            "created_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        }


class ResultsStore:
    """Appends and reads measured results at a JSON Lines path."""

    def __init__(self, path: Path) -> None:
        """Open the store at ``path`` (created on first record)."""
        self._path = path
        # A concurrent run has more than one thread finishing a task at once;
        # this guards the append-then-update-bookkeeping sequence in
        # ``record`` so two writers can never interleave their file writes or
        # race on ``_seen``/``_latest_status``.
        self._lock = threading.Lock()
        records = self._records()
        self._seen: set[tuple[str, str]] = {(str(r["task_id"]), str(r["run_id"])) for r in records}
        # Last write wins per task_id, regardless of run_id -- file order is
        # chronological (append-only), so a later record always overwrites.
        self._latest_status: dict[str, str] = {str(r["task_id"]): str(r["status"]) for r in records}

    def _records(self) -> list[dict[str, object]]:
        """Every record on disk, in file (chronological) order."""
        if not self._path.exists():
            return []
        records = []
        for line in self._path.read_text().splitlines():
            text = line.strip()
            if text:
                records.append(json.loads(text))
        return records

    def _latest(self) -> dict[tuple[str, str], dict[str, object]]:
        """The last-written record for each (task_id, run_id)."""
        records: dict[tuple[str, str], dict[str, object]] = {}
        for record in self._records():
            records[(str(record["task_id"]), str(record["run_id"]))] = record
        return records

    def is_done(self, task_id: str, run_id: str) -> bool:
        """Whether this task already has a result for this specific run."""
        return (task_id, run_id) in self._seen

    def should_skip(self, task_id: str) -> bool:
        """Whether this task already succeeded, under any run.

        A failed latest attempt is not skip-worthy: it is exactly the case a
        later run (after a fix) needs the chance to overturn.
        """
        return self._latest_status.get(task_id) in _SETTLED_STATUSES

    def record(self, result: Result) -> None:
        """Append a task's result for its run."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result.to_json()) + "\n")
            self._seen.add((result.task_id, result.run_id))
            self._latest_status[result.task_id] = result.status

    def rows(self, run_id: str) -> list[dict[str, object]]:
        """Every latest result for a run, as plain dicts."""
        return [rec for rec in self._latest().values() if rec["run_id"] == run_id]

    def run_ids(self) -> list[str]:
        """Every run id present, most rows first."""
        counts = Counter(str(rec["run_id"]) for rec in self._latest().values())
        return [run_id for run_id, _ in counts.most_common()]

    def close(self) -> None:
        """Present for symmetry with a connection-based store; a no-op here."""
