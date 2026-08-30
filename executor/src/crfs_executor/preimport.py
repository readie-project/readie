"""Startup imports, and the signal that the process is worth capturing.

This is what a checkpoint is *for*. The pipeline runs a sandbox with a chosen
set of modules named in ``CRFS_PREIMPORT``, triggers gVisor's internal checkpointing, 
and captures the process while those modules are resident. A later restore skips the
import cost that dominates an otherwise cold start.
"""

from __future__ import annotations

import importlib
import sys
import time
import os
from dataclasses import dataclass, field


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


def trigger_checkpoint(report: PreimportReport | None = None) -> str | None:
    if report is not None:
        print(
            f"[preimport] {len(report.loaded)} loaded, "
            f"{len(report.failed)} failed, {report.elapsed:.2f}s",
            flush=True,
        )

    path = "/proc/gvisor/checkpoint"
    try:
        # Open the file descriptor for reading and writing
        fd = os.open(path, os.O_RDWR)
        
        # Write '1' to trigger the checkpoint
        os.write(fd, b"1")
        
        # Read blocks the thread until the checkpoint completes.
        # It returns 'resume', 'restore', or 'error'.
        result_bytes = os.read(fd, 1024)
        result = result_bytes.decode('utf-8').strip()
        
        os.close(fd)
        return result
    except FileNotFoundError:
        print(f"[checkpoint] '{path}' not found", file=sys.stderr)
        return None
    except PermissionError:
        print(f"[checkpoint] Permission denied", file=sys.stderr)
        return None
    except OSError as exc:
        print(f"[checkpoint] OS Error: {exc}", file=sys.stderr)
        return None
