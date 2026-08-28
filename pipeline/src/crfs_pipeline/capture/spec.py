"""Generate the sandbox's OCI configuration.

The specification is produced by the ``ocispec`` helper, which is built from the
worker's own spec builder. That indirection is deliberate: a gVisor checkpoint
only restores into a sandbox whose shape matches the one it was captured from,
and two independent generators cannot be kept in agreement by review. Calling
the worker's code here makes agreement structural.

It also fixes, by construction, three things the original hand-written config
got wrong: EXECUTOR_DIR was never placed in the sandbox environment (so the
executor exited immediately on restore), no mount existed for the socket
directory (so the worker could never reach it), and ``runsc spec`` defaults
``process.terminal`` to true (so the captured process had pty stdio, which
cannot be re-supplied at restore).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from crfs_pipeline.config import (
    EXECUTOR_ARGV,
    SANDBOX_EXECUTOR_DIR,
    Settings,
)


class SpecError(Exception):
    """The sandbox specification could not be produced."""


def build_config(settings: Settings, *, preimport: str = "") -> str:
    """Write ``<bundle>/config.json`` and return the spec fingerprint.

    ``preimport`` is the only thing that varies between the checkpoints of one
    generation. It travels in ``process.env``, which ``runsc.Fingerprint``
    excludes deliberately -- so every checkpoint here shares one fingerprint and
    all of them restore into the worker's sandbox. A Go test pins that
    exclusion, because adding env to the fingerprint would invalidate every
    generation at once.
    """
    settings.socket_dir.mkdir(parents=True, exist_ok=True)

    params = {
        "id": "checkpoint-builder",
        "bundle_dir": str(settings.bundle_dir),
        "rootfs_path": str(settings.rootfs_path),
        # Paired with the overlay: the sandbox gets copy-on-write over a shared
        # rootfs, so root is writable but the shared tree is never touched.
        "root_readonly": False,
        "args": list(EXECUTOR_ARGV),
        "env": [
            f"EXECUTOR_DIR={SANDBOX_EXECUTOR_DIR}",
            f"PYTHONPATH={settings.rootfs_pythonpath}",
            "EXECUTOR_MODE=capture",
            f"CRFS_PREIMPORT={preimport}",
        ],
        "cwd": "/",
        "mounts": [
            {
                "source": str(settings.socket_dir),
                "destination": SANDBOX_EXECUTOR_DIR,
                "type": "bind",
                "options": ["rbind", "rw"],
            }
        ],
        "cpu_quota": 50000,
        "cpu_period": 100000,
        "pids_limit": 100,
        "overlay": settings.sandbox_overlay,
        "network": settings.sandbox_network,
        # A gpu generation captures under nvproxy, which the worker must restore
        # under. gpu is part of the fingerprint, so this keeps the two in step.
        "gpu": settings.flavor == "gpu",
    }

    params_path = settings.bundle_dir / "ocispec-params.json"
    params_path.write_text(json.dumps(params, indent=2))

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [settings.ocispec_binary, "-params", str(params_path), "-print"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        msg = (
            f"{settings.ocispec_binary} not found; it is built from the worker's "
            f"cmd/ocispec and installed by pipeline/Dockerfile"
        )
        raise SpecError(msg) from exc
    except subprocess.CalledProcessError as exc:
        msg = f"ocispec failed: {exc.stderr.strip()}"
        raise SpecError(msg) from exc
    finally:
        params_path.unlink(missing_ok=True)

    return completed.stdout.strip()


def runsc_version(binary: str = "runsc") -> str:
    """Return the runtime version, which a checkpoint is bound to.

    gVisor's save format is not stable across releases, so this is recorded with
    every checkpoint and checked before a restore is attempted.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [binary, "--version"], check=True, capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        msg = f"{binary} not found; checkpoints can only be captured where gVisor is installed"
        raise SpecError(msg) from exc
    except subprocess.CalledProcessError as exc:
        msg = f"{binary} --version failed: {exc.stderr.strip()}"
        raise SpecError(msg) from exc

    lines = completed.stdout.strip().splitlines()
    if not lines:
        msg = f"{binary} --version printed nothing"
        raise SpecError(msg)
    return lines[0]


def bundle_is_ready(settings: Settings) -> bool:
    """Whether a bundle exists to run. Checked before a build, not during one."""
    return (settings.bundle_dir / "config.json").is_file() and Path(settings.rootfs_path).is_dir()
