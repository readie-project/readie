"""Entry point: ``python -m readie_executor``.

The startup order is the whole design. Pre-import, announce, sleep to be
captured, and only then bind. Binding earlier would put the socket inode in the
checkpoint image, and a restored sandbox would come back holding a socket
attached to a worker that no longer exists.
"""

from __future__ import annotations

import os
import sys

from readie_executor.config import ConfigError, Settings, get_current_mode
from readie_executor.preimport import trigger_checkpoint, preimport
from readie_executor.server import ExecutorServer


def reload_gvisor_envs():
    """Reads updated config.json envs injected by gVisor into spec_environ."""

    spec_environ_path = "/proc/gvisor/spec_environ"
    if os.path.exists(spec_environ_path):
        print(f"[executor] Updating envs from {spec_environ_path}", flush=True)
        with open(spec_environ_path, "r") as f:
            # Lines are null-byte (\x00) separated in linux proc files
            env_entries = f.read().split("\0")
            for entry in env_entries:
                if "=" in entry:
                    key, val = entry.split("=", 1)
                    os.environ[key] = val


def main(argv: list[str] | None = None) -> int:
    """Run the executor. Returns a process exit code."""
    del argv  # No arguments: the sandbox configures this entirely through env.

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"[executor] {exc}", file=sys.stderr, flush=True)
        return 2

    status = ""
    # Ordinary worker sandboxes stream their merged stdout/stderr to the
    # client. Keep capture progress out of that stream; a restored checkpoint
    # resumes after this branch.
    if get_current_mode() == "capture":
        report = preimport(settings.preimport)
        status = trigger_checkpoint(report)

    if status == "error":
        print("[executor] Cannot restore sandbox")
        return 1

    if status == "restore":
        reload_gvisor_envs()

    if get_current_mode() == "measure":
        return 0

    server = ExecutorServer(settings)
    try:
        server.bind()
    except OSError as exc:
        print(f"[executor] could not bind {settings.socket_path}: {exc}", file=sys.stderr)
        return 1

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
