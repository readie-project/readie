"""Make an item's code runnable, and do the same for a corpus that already exists.

Two steps, both only ever changing code that could not run as it was:

1. the mechanical fixes in :mod:`runnable` (notebook syntax, folders), which are
   cheap and always applied;
2. the stand-in data rewrite in :mod:`standins`, which costs model calls and is
   applied to items that read files that do not exist.

Resources are never removed by either. An item that read a Kaggle dataset still
lists it after the dataset reads were replaced, because the resource describes
what the workload is about, and only the imports a rewrite adds are unioned in.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path

from readie_pipeline.corpus.dependencies import UNPARSABLE, missing_inputs, scan
from readie_pipeline.corpus.extract import CodeError, extract_resources
from readie_pipeline.corpus.models import Corpus, CorpusError, CorpusItem
from readie_pipeline.corpus.runnable import clean_steps, strip_prelude
from readie_pipeline.corpus.standins import Outcome, Result, StandIns

#: Outcome recorded when a rewrite imported something that does not exist.
RESOURCE_REJECTED = "resource check failed"


def merge_resources(
    resources: tuple[str, ...],
    steps: Sequence[str],
    cells: Sequence[int],
    verify: Callable[[tuple[str, ...]], list[str] | str],
) -> tuple[str, ...] | None:
    """The item's resources plus what the rewritten cells import, or ``None``.

    Nothing already listed is dropped. A new resource must exist, and ``verify``
    returns the ones that do not, or a reason when it could not check; either way
    the rewrite is refused, because it imported something that is not real.
    """
    try:
        found = extract_resources([strip_prelude(steps[i]) for i in cells])
    except CodeError:
        return None
    new = tuple(sorted(set(found) - set(resources)))
    if not new:
        return resources
    if verify(new):
        return None
    return tuple(sorted({*resources, *new}))


def code_key(code: Sequence[str]) -> str:
    """A stable key for a piece of code."""
    return hashlib.sha256("\n".join(code).encode()).hexdigest()


class RewriteLog:
    """Which code has already been offered to the model, and how it went.

    A rewrite that fails costs model calls, and a resumed clean pass would repeat
    every one of them. The log lets it skip those, unless asked to retry.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._outcomes: dict[str, str] = {}
        if path is not None and path.exists():
            try:
                raw = json.loads(path.read_text())
            except json.JSONDecodeError:
                raw = {}
            if isinstance(raw, dict):
                self._outcomes = {str(k): str(v) for k, v in raw.items()}

    def attempted(self, code: Sequence[str]) -> bool:
        """Whether this code was offered to the model before."""
        return code_key(code) in self._outcomes

    def record(self, code: Sequence[str], outcome: str) -> None:
        """Remember what happened to this code."""
        self._outcomes[code_key(code)] = outcome

    def flush(self) -> None:
        """Write the log to disk."""
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".partial")
        temp.write_text(json.dumps(self._outcomes, indent=0, sort_keys=True) + "\n")
        temp.replace(self._path)


@dataclass(slots=True)
class CleanReport:
    """What a clean pass did."""

    items: int = 0
    changed: int = 0
    fixes: Counter[str] = field(default_factory=Counter)
    rewrites: Counter[str] = field(default_factory=Counter)
    already_attempted: int = 0
    still_missing: int = 0
    unparsable: int = 0


def clean_corpus(
    path: Path,
    *,
    standins: StandIns | None = None,
    verify: Callable[[tuple[str, ...]], list[str] | str] | None = None,
    log: RewriteLog | None = None,
    workers: int = 6,
    retry: bool = False,
    save_every: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
    on_line: Callable[[str], None] = print,
) -> CleanReport:
    """Clean every item of the corpus at ``path`` in place.

    Mechanical fixes go first and are saved at once. Then, with ``standins``, the
    items that read missing files are rewritten in parallel, the corpus is saved
    at least every ``save_every`` seconds while that runs, and ``log`` remembers
    each outcome so an interrupted pass resumes where it stopped.
    """
    corpus = Corpus.load(path)
    items = list(corpus.items)
    report = CleanReport(items=len(items))

    for index, item in enumerate(items):
        cleaned = clean_steps(item.code)
        if cleaned.code != item.code:
            items[index] = replace(item, code=cleaned.code)
            report.changed += 1
            report.fixes.update(set(cleaned.changes))
    Corpus.of(items).save(path)
    on_line(f"[*] mechanical fixes changed {report.changed} of {len(items)} items")

    if standins is not None and verify is not None:
        _RewritePass(
            standins=standins,
            verify=verify,
            log=log or RewriteLog(None),
            workers=workers,
            retry=retry,
            save_every=save_every,
            clock=clock,
            on_line=on_line,
        ).run(path, items, report)

    for item in items:
        if missing_inputs(item.code):
            report.still_missing += 1
    report.unparsable = sum(1 for item in items if _has_unparsable_cell(item))
    Corpus.of(items).save(path)
    return report


def _has_unparsable_cell(item: CorpusItem) -> bool:
    return any(f.kind == UNPARSABLE for f in scan(item.code))


@dataclass(frozen=True, slots=True)
class _RewritePass:
    """The settings of one stand-in rewrite pass over a corpus."""

    standins: StandIns
    verify: Callable[[tuple[str, ...]], list[str] | str]
    log: RewriteLog
    workers: int
    retry: bool
    save_every: float
    clock: Callable[[], float]
    on_line: Callable[[str], None]

    def run(self, path: Path, items: list[CorpusItem], report: CleanReport) -> None:
        """Rewrite what needs it, saving as it goes."""
        targets = self._targets(items, report)
        self.on_line(
            f"[*] {len(targets)} items read files that do not exist; "
            f"{report.already_attempted} were tried before and are skipped"
        )
        if not targets:
            return

        last_save = self.clock()
        pool = ThreadPoolExecutor(max_workers=self.workers)
        try:
            futures = {pool.submit(_safe_rewrite, self.standins, items[i].code): i for i in targets}
            for done, future in enumerate(as_completed(futures), start=1):
                self._record(items, futures[future], future.result(), report)
                if done % 25 == 0 or done == len(targets):
                    rewritten = report.rewrites[Outcome.REWRITTEN.value]
                    self.on_line(f"    {done} of {len(targets)} tried, {rewritten} rewritten")
                if self.clock() - last_save >= self.save_every:
                    Corpus.of(items).save(path)
                    self.log.flush()
                    last_save = self.clock()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            Corpus.of(items).save(path)
            self.log.flush()

    def _targets(self, items: list[CorpusItem], report: CleanReport) -> list[int]:
        targets: list[int] = []
        for index, item in enumerate(items):
            if item.original is not None or not missing_inputs(item.code):
                continue
            if not self.retry and self.log.attempted(item.code):
                report.already_attempted += 1
                continue
            targets.append(index)
        return targets

    def _record(
        self, items: list[CorpusItem], index: int, result: Result, report: CleanReport
    ) -> None:
        item = items[index]
        outcome = result.outcome.value
        if result.outcome is Outcome.REWRITTEN:
            resources = merge_resources(item.resources, result.code, result.cells, self.verify)
            if resources is None:
                outcome = RESOURCE_REJECTED
            else:
                items[index] = replace(
                    item, code=result.code, resources=resources, original=item.code
                )
        report.rewrites[outcome] += 1
        # A failed model call is usually transient, so it is retried on the next
        # pass; every other outcome is final for this code and is remembered.
        if result.outcome is not Outcome.MODEL_FAILED:
            self.log.record(item.code, outcome)


def _safe_rewrite(standins: StandIns, code: tuple[str, ...]) -> Result:
    """A rewrite that never raises: a failed call keeps the original."""
    try:
        return standins.rewrite(code)
    except CorpusError:
        return Result(Outcome.MODEL_FAILED, code)
    except Exception:  # noqa: BLE001 - losing a rewrite is fine, losing the pass is not
        return Result(Outcome.MODEL_FAILED, code)
