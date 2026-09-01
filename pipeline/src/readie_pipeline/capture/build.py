"""Capture one gVisor checkpoint per planned set."""

from __future__ import annotations

import contextlib
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from readie_pipeline.config import Settings
from readie_pipeline.planning.ports import CheckpointPlan
from readie_pipeline.capture.spec import ExecutorMode


class CaptureError(Exception):
    """A checkpoint could not be captured."""


def _runsc(settings: Settings, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a runsc command.

    Global flags must precede the subcommand: runsc parses with stdlib flag
    semantics, which stop at the first non-flag argument.
    """
    cmd = [settings.runsc_binary, *settings.global_runsc_flags, *args]
    return subprocess.run(cmd, check=check, capture_output=True, text=True)  # noqa: S603 - fixed argv, no shell


def _delete(settings: Settings, container_id: str) -> None:
    """Discard a sandbox, ignoring failures.

    Without this the sandbox is orphaned and a second build run collides on the
    container id.
    """
    with contextlib.suppress(OSError):
        _runsc(settings, "delete", "--force", container_id, check=False)


def capture(
    settings: Settings,
    checkpoint_id: str,
    plan: CheckpointPlan,
    *,
    write_spec: Callable[[str, ExecutorMode, str], None],
    on_line: Callable[[str], None] = print,
) -> Path:
    """Capture one checkpoint with a set of packages already imported.

    The imports reach the executor through ``READIE_PREIMPORT`` in the sandbox
    spec. They used to be prepended to the executor's source inside the shared
    rootfs and restored in a ``finally``, so a build killed in between left that
    tree mutated for the next run.
    """

    destination = settings.checkpoints_dir / checkpoint_id
    destination.mkdir(parents=True, exist_ok=True)
    write_spec("capture", destination, ",".join(plan.imports))

    # A stale sandbox under this id would make `runsc run` fail immediately.
    _delete(settings, checkpoint_id)

    try:
        result = _runsc(settings, "run", f"--bundle={settings.bundle_dir}", checkpoint_id)
        if result.stdout.strip():
            on_line(result.stdout.strip())
    except subprocess.CalledProcessError as exc:
        msg = f"{checkpoint_id}: runsc checkpoint failed: {exc.stderr.strip()}"
        raise CaptureError(msg) from exc
    finally:
        # The sandbox is never reaped by the wait above, so tear it down
        # regardless of how we leave.
        _delete(settings, checkpoint_id)

    return destination


def measure_time(
    settings: Settings,
    checkpoint_id: str,
    *,
    write_spec: Callable[[str, ExecutorMode, str], None],
) -> float:
    """Restore one checkpoint with a set of packages already imported and measure its time."""

    destination = settings.checkpoints_dir / checkpoint_id
    write_spec("measure", destination, "")

    # A stale sandbox under this id would make `runsc restore` fail immediately.
    _delete(settings, checkpoint_id)

    try:
        start_time = time.perf_counter()
        # _runsc(settings, "create", f"--bundle={settings.bundle_dir}", checkpoint_id)
        _runsc(settings, "restore", f"--bundle={settings.bundle_dir}", f"--image-path={destination}", checkpoint_id)
        end_time = time.perf_counter()
        return end_time - start_time
    except subprocess.CalledProcessError as exc:
        msg = f"{checkpoint_id}: runsc restore failed: {exc.stderr.strip()}"
        raise CaptureError(msg) from exc
    finally:
        # The sandbox is never reaped by the wait above, so tear it down
        # regardless of how we leave.
        _delete(settings, checkpoint_id)
