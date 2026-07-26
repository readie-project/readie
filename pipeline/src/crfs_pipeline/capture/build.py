"""Capture one gVisor checkpoint per planned set."""

from __future__ import annotations

import contextlib
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from crfs_pipeline.config import Settings
from crfs_pipeline.planning.ports import CheckpointPlan

#: Printed by the executor once its pre-imports have run and before it sleeps.
#: The only signal that the process is worth capturing, and a contract with
#: crfs_executor.config.READY_SENTINEL.
READY_SENTINEL = "READY_FOR_CHECKPOINT"


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


def _await_ready(
    process: subprocess.Popen[str],
    *,
    timeout: float,
    on_line: Callable[[str], None],
) -> bool:
    """Wait for the ready sentinel, or give up.

    The reading happens on another thread. A sandbox that produces no output at
    all blocks forever in ``readline``, so a deadline checked *inside* the read
    loop -- as the previous implementation did -- can never fire in exactly the
    case it was written to bound.
    """
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        stream = process.stdout
        if stream is None:  # pragma: no cover - Popen was given stdout=PIPE
            lines.put(None)
            return
        try:
            for line in iter(stream.readline, ""):
                lines.put(line)
        except ValueError:
            # The caller closed the pipe while this thread was blocked in
            # readline, which is what happens on the timeout path.
            pass
        finally:
            lines.put(None)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            return False

        if line is None:
            return False  # the sandbox exited without announcing itself

        on_line(line.rstrip())
        if READY_SENTINEL in line:
            return True


def capture(
    settings: Settings,
    checkpoint_id: str,
    plan: CheckpointPlan,
    *,
    write_spec: Callable[[str], None],
    on_line: Callable[[str], None] = print,
) -> Path:
    """Capture one checkpoint with a set of packages already imported.

    The imports reach the executor through ``CRFS_PREIMPORT`` in the sandbox
    spec. They used to be prepended to the executor's source inside the shared
    rootfs and restored in a ``finally``, so a build killed in between left that
    tree mutated for the next run.
    """
    write_spec(",".join(plan.imports))

    destination = settings.checkpoints_dir / checkpoint_id
    destination.mkdir(parents=True, exist_ok=True)

    # A stale sandbox under this id would make `runsc run` fail immediately.
    _delete(settings, checkpoint_id)

    # `with` so the stdout pipe is closed however this exits. A build captures
    # several checkpoints in a loop, and a leaked pipe per iteration adds up.
    with subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [settings.runsc_binary, *settings.global_runsc_flags, "run", checkpoint_id],
        cwd=settings.bundle_dir,  # runsc takes the bundle from the working directory
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as process:
        try:
            ready = _await_ready(process, timeout=settings.ready_timeout, on_line=on_line)
            if not ready:
                msg = (
                    f"{checkpoint_id}: {READY_SENTINEL} not seen within "
                    f"{settings.ready_timeout:g}s; the sandbox produced no usable output"
                )
                raise CaptureError(msg)

            result = _runsc(settings, "checkpoint", f"--image-path={destination}", checkpoint_id)
            if result.stdout.strip():
                on_line(result.stdout.strip())
        except subprocess.CalledProcessError as exc:
            msg = f"{checkpoint_id}: runsc checkpoint failed: {exc.stderr.strip()}"
            raise CaptureError(msg) from exc
        finally:
            # The sandbox is never reaped by the wait above, so tear it down
            # regardless of how we leave.
            _delete(settings, checkpoint_id)
            if process.poll() is None:
                process.kill()

    return destination
