"""Bootstrapping the sandbox's network before a call needs it.

Nothing populates /etc/resolv.conf for a gVisor sandbox the way a normal
container runtime would at container-create time, so without this every DNS
lookup -- a package install's, or a task's own -- fails.
"""

from __future__ import annotations

import shutil
from pathlib import Path

#: Where the rootfs bakes a working nameserver config (see pipeline/Dockerfile).
#: Not baked at /etc/resolv.conf directly -- Docker treats that exact path as a
#: runtime-managed file and drops anything written to it at build time.
_RESOLV_CONF_DEFAULT = Path("/etc/readie-resolv.conf")
_RESOLV_CONF = Path("/etc/resolv.conf")


def ensure_dns() -> None:
    """Copy in a working /etc/resolv.conf if the rootfs shipped an empty one.

    Called unconditionally for every call, not just one that installs
    packages: a task that downloads its own dataset needs this exactly as
    much as `uv` does. Best-effort: an older rootfs built before
    _RESOLV_CONF_DEFAULT existed just leaves this a no-op rather than failing
    the call outright.
    """
    try:
        if _RESOLV_CONF.stat().st_size == 0 and _RESOLV_CONF_DEFAULT.exists():
            shutil.copyfile(_RESOLV_CONF_DEFAULT, _RESOLV_CONF)
    except OSError:
        pass
