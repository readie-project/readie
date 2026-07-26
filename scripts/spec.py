"""Generate the sandbox's OCI configuration.

The specification is produced by the `ocispec` helper, which is built from the
worker's own spec builder. That indirection is deliberate: a gVisor checkpoint
only restores into a sandbox whose shape matches the one it was captured from,
and two independent generators cannot be kept in agreement by review. Calling
the worker's code here makes agreement structural.

It also fixes, by construction, three things the previous hand-written config
got wrong: EXECUTOR_DIR was never placed in the sandbox environment (so the
executor exited immediately on restore), no mount existed for the socket
directory (so the worker could never reach it), and `runsc spec` defaults
process.terminal to true (so the captured process had pty stdio, which cannot
be re-supplied at restore).
"""

import json
import os
import subprocess

# Paths inside the sandbox. These are a contract with the executor and with
# every checkpoint already captured.
SANDBOX_EXECUTOR_DIR = "/tmp"

OCISPEC_BINARY = os.environ.get("OCISPEC_BINARY", "/usr/local/bin/ocispec")


def build_config(
    bundle_dir: str,
    rootfs_path: str,
    executor_entrypoint: str,
    python_path: str,
    socket_dir: str,
    container_id: str = "checkpoint-builder",
    overlay: str = "root:memory",
    network: str = "none",
    cpu_quota: int = 50000,
    cpu_period: int = 100000,
    pids_limit: int = 100,
) -> str:
    """Write <bundle_dir>/config.json and return the spec fingerprint."""
    params = {
        "id": container_id,
        "bundle_dir": bundle_dir,
        "rootfs_path": rootfs_path,
        # Paired with the overlay: the sandbox gets copy-on-write over a shared
        # rootfs, so root is writable but the shared tree is never touched.
        "root_readonly": False,
        "args": ["python", "-u", executor_entrypoint],
        "env": [
            f"EXECUTOR_DIR={SANDBOX_EXECUTOR_DIR}",
            f"PYTHONPATH={python_path}",
        ],
        "cwd": "/",
        "mounts": [
            {
                "source": socket_dir,
                "destination": SANDBOX_EXECUTOR_DIR,
                "type": "bind",
                "options": ["rbind", "rw"],
            }
        ],
        "cpu_quota": cpu_quota,
        "cpu_period": cpu_period,
        "pids_limit": pids_limit,
        "overlay": overlay,
        "network": network,
    }

    params_path = os.path.join(bundle_dir, "ocispec-params.json")
    with open(params_path, "w") as f:
        json.dump(params, f, indent=2)

    try:
        result = subprocess.run(
            [OCISPEC_BINARY, "-params", params_path, "-print"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        print(e.stderr)
        raise
    finally:
        os.remove(params_path)

    return result.stdout.strip()


def runsc_version() -> str:
    """Return the runtime version, which a checkpoint is bound to.

    gVisor's save format is not stable across releases, so this is recorded
    with every checkpoint and checked before a restore is attempted.
    """
    result = subprocess.run(
        ["runsc", "--version"], check=True, capture_output=True, text=True
    )
    return result.stdout.strip().splitlines()[0]
