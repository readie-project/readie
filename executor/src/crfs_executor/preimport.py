"""Startup imports, and the signal that the process is worth capturing.

This is what a checkpoint is *for*. The pipeline runs a sandbox with a chosen
set of modules named in ``CRFS_PREIMPORT``, waits for the ready sentinel, and
captures the process while those modules are resident. A later restore skips the
import cost that dominates an otherwise cold start.
"""

from __future__ import annotations

import importlib
import sys
import time
from dataclasses import dataclass, field

from crfs_executor.config import READY_SENTINEL


@dataclass(slots=True)
class PreimportReport:
    """Which modules loaded, which did not, and how long it took."""

    loaded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    elapsed: float = 0.0


def preimport(modules: tuple[str, ...]) -> PreimportReport:
    """Import each module, continuing past failures.

    A failed import is reported and skipped rather than raised. The alternative
    is that one bad name in a planned set aborts the whole capture, and the
    operator sees a sandbox that exited instead of a checkpoint that is merely
    missing one module — the pipeline can only tell those apart if we keep
    going and say so.
    """
    report = PreimportReport()
    started = time.perf_counter()

    for name in modules:
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 - a module may raise anything at import
            # Including SystemExit: some scientific packages call sys.exit on an
            # unsupported platform, and that must not take the executor with it.
            report.failed[name] = f"{type(exc).__name__}: {exc}"
            print(f"[preimport] {name} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            # Raise so that it propogates to the pipeline
            raise
        else:
            report.loaded.append(name)

    report.elapsed = time.perf_counter() - started
    return report


def announce_ready(report: PreimportReport | None = None) -> None:
    """Print the sentinel the pipeline waits for.

    Flushed explicitly. The pipeline reads this pipe line by line, and a
    buffered sentinel is a capture that times out for no visible reason.
    """
    if report is not None:
        print(
            f"[preimport] {len(report.loaded)} loaded, "
            f"{len(report.failed)} failed, {report.elapsed:.2f}s",
            flush=True,
        )
    print(READY_SENTINEL, flush=True)


def await_checkpoint(seconds: float) -> None:
    """Sit still long enough to be captured.

    The sleep *is* the capture window. A restored sandbox resumes part-way
    through it and finishes the remainder before binding its socket, which is
    why the worker's dial budget has to exceed this.
    """
    if seconds > 0:
        time.sleep(seconds)
