"""The corpus store: fetched and adapted tasks on disk.

One JSON object per line (JSONL), append-only. A line is a complete record, so an
interrupted fetch leaves a readable corpus and the next run continues. Tasks dedupe
by id, so re-fetching an identical snippet adds nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from readie_evals.models import Task


class CorpusStore:
    """Reads and appends the task corpus at a path."""

    def __init__(self, path: Path) -> None:
        """Store the corpus at ``path`` (created on first append)."""
        self._path = path

    def load(self) -> list[Task]:
        """Every task on disk, deduped by id with the first occurrence winning."""
        if not self._path.exists():
            return []
        seen: dict[str, Task] = {}
        for index, line in enumerate(self._path.read_text().splitlines()):
            text = line.strip()
            if not text:
                continue
            task = Task.from_json(json.loads(text), index=index)
            seen.setdefault(task.id, task)
        return list(seen.values())

    def existing_ids(self) -> set[str]:
        """The ids already in the corpus."""
        return {task.id for task in self.load()}

    def append(self, tasks: list[Task]) -> int:
        """Append tasks whose id is not already present. Returns how many were added."""
        known = self.existing_ids()
        fresh = [task for task in tasks if task.id not in known]
        if not fresh:
            return 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            for task in fresh:
                handle.write(json.dumps(task.to_json()) + "\n")
        return len(fresh)

    def counts(self) -> dict[tuple[str, str], int]:
        """How many tasks exist per (source, category)."""
        counts: dict[tuple[str, str], int] = {}
        for task in self.load():
            key = (task.source, task.category)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def deficit(self, targets: dict[tuple[str, str], int]) -> dict[tuple[str, str], int]:
        """How many more tasks each (source, category) needs to hit its target."""
        have = self.counts()
        return {
            key: target - have.get(key, 0)
            for key, target in targets.items()
            if target - have.get(key, 0) > 0
        }
