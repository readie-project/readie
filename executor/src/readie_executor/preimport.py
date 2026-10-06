"""Startup imports, and the signal that the process is worth capturing.

This is what a checkpoint is *for*. The pipeline runs a sandbox with a chosen
set of modules named in ``READIE_PREIMPORT``, triggers gVisor's internal checkpointing,
and captures the process while those modules are resident. A later restore skips the
import cost that dominates an otherwise cold start.
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class PreimportReport:
    """Which modules loaded, which did not, and how long it took."""

    loaded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    elapsed: float = 0.0


def preimport(modules: tuple[str, ...]) -> PreimportReport:
    """Import each module, stopping and raising on the first failure."""
    report = PreimportReport()
    started = time.perf_counter()

    for name in modules:
        try:
            importlib.import_module(name)
        except BaseException as exc:
            # Including SystemExit: some scientific packages call sys.exit on an
            # unsupported platform, and that must not take the executor with it
            # silently - it still needs to surface as a hard preimport failure.
            report.failed[name] = f"{type(exc).__name__}: {exc}"
            print(f"[preimport] {name} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            raise
        else:
            report.loaded.append(name)

    report.elapsed = time.perf_counter() - started
    return report


def trigger_checkpoint(report: PreimportReport | None = None) -> str | None:
    """Trigger gVisor's internal checkpoint via ``/proc/gvisor/checkpoint``.

    Returns 'resume', 'restore', or None if the trigger could not be reached.
    """
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
        result = result_bytes.decode("utf-8").strip()

        os.close(fd)
    except FileNotFoundError:
        print(f"[checkpoint] '{path}' not found", file=sys.stderr)
        return None
    except PermissionError:
        print("[checkpoint] Permission denied", file=sys.stderr)
        return None
    except OSError as exc:
        print(f"[checkpoint] OS Error: {exc}", file=sys.stderr)
        return None
    else:
        return result
