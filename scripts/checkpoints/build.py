import json
import os
import subprocess
import time

from .types import Checkpoint

# READY_SENTINEL is printed by the executor once its pre-imports have run and
# before it sleeps. It is the only signal that the process is worth capturing.
READY_SENTINEL = "READY_FOR_CHECKPOINT"

# READY_TIMEOUT bounds the wait for that sentinel. Without it a sandbox that
# fails to start leaves this loop reading until EOF and the build hangs.
READY_TIMEOUT = 300


def _runsc(global_flags, *args, check=True):
    """Run a runsc command.

    Global flags must precede the subcommand: runsc parses with stdlib flag
    semantics, which stop at the first non-flag argument.
    """
    cmd = ["runsc", *global_flags, *args]
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _delete(global_flags, container_id):
    """Discard a sandbox, ignoring failures.

    Without this the sandbox is orphaned and a second build run collides on the
    container id.
    """
    try:
        _runsc(global_flags, "delete", "--force", container_id, check=False)
    except OSError:
        pass


def build_checkpoint(
    id: str,
    executor_code: str,
    executor_path: str,
    checkpoints_dir: str,
    checkpoint: Checkpoint,
    global_flags=(),
    runsc_version: str = "",
    spec_fingerprint: str = "",
    rootfs_id: str = "",
    generation_id: str = "",
):
    """Capture one checkpoint of the executor with a set of packages imported.

    The imports are prepended to the executor source *inside the rootfs*, so
    the captured process has them resident in memory. Writing them anywhere
    else produces a checkpoint of a bare executor, which restores fine and
    saves nothing.
    """
    imports = "\n".join([f"import {imp}" for imp in checkpoint["imports"]])

    # Add datasets, models, tokenizers to the code as variables

    with open(executor_path, "w") as f:
        f.write(imports + "\n\n" + executor_code)

    checkpoint_path = os.path.join(checkpoints_dir, id)
    os.makedirs(checkpoint_path, exist_ok=True)

    # A stale sandbox under this id would make `runsc run` fail immediately.
    _delete(global_flags, id)

    process = subprocess.Popen(
        ["runsc", *global_flags, "run", id],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    captured = False
    deadline = time.monotonic() + READY_TIMEOUT
    try:
        for line in iter(process.stdout.readline, ""):
            print(line.rstrip())
            if READY_SENTINEL in line:
                result = _runsc(
                    global_flags, "checkpoint", f"--image-path={checkpoint_path}", id
                )
                print(result.stdout)
                captured = True
                break
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"{id}: {READY_SENTINEL} not seen within {READY_TIMEOUT}s"
                )
    finally:
        # The sandbox is never reaped by the read loop above, so tear it down
        # regardless of how we leave.
        _delete(global_flags, id)
        if process.poll() is None:
            process.kill()
        process.wait()

    if not captured:
        raise RuntimeError(f"{id}: sandbox exited before printing {READY_SENTINEL}")

    _write_meta(
        checkpoint_path,
        id=id,
        generation_id=generation_id,
        runsc_version=runsc_version,
        spec_fingerprint=spec_fingerprint,
        rootfs_id=rootfs_id,
        imports=checkpoint["imports"],
    )

    return checkpoint_path


def _write_meta(
    checkpoint_path,
    id,
    generation_id,
    runsc_version,
    spec_fingerprint,
    rootfs_id,
    imports,
):
    """Record what this checkpoint can be restored into.

    The worker reads this before attempting a restore, so a mismatch becomes a
    named, logged downgrade rather than an opaque failure minutes into a
    request.
    """
    meta = {
        "checkpoint_id": id,
        "generation_id": generation_id,
        "producer": "scripts",
        "runsc_version": runsc_version,
        "spec_fingerprint": spec_fingerprint,
        "rootfs_id": rootfs_id,
        "imports": list(imports),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(os.path.join(checkpoint_path, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
