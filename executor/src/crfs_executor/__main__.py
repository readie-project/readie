"""Entry point: ``python -m crfs_executor``.

The startup order is the whole design. Pre-import, announce, sleep to be
captured, and only then bind. Binding earlier would put the socket inode in the
checkpoint image, and a restored sandbox would come back holding a socket
attached to a worker that no longer exists.
"""

from __future__ import annotations

import sys

from crfs_executor.config import ConfigError, Settings
from crfs_executor.preimport import announce_ready, await_checkpoint, preimport
from crfs_executor.server import ExecutorServer


def main(argv: list[str] | None = None) -> int:
    """Run the executor. Returns a process exit code."""
    del argv  # No arguments: the sandbox configures this entirely through env.

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"[executor] {exc}", file=sys.stderr, flush=True)
        return 2

    # Ordinary worker sandboxes stream their merged stdout/stderr to the
    # client. Keep capture progress out of that stream; a restored checkpoint
    # resumes after this branch.
    if settings.capture_mode:
        report = preimport(settings.preimport)
        announce_ready(report)
        await_checkpoint(settings.checkpoint_sleep)

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
