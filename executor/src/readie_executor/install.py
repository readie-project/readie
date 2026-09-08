"""Installing packages with ``uv`` before a call runs.

Every request restores a fresh, isolated container (warm reuse is currently
disabled worker-side), so there is nothing to check an "already installed"
package against -- this always shells out, even for a package the rootfs
already carries.

This only rewrites files on disk. The restored interpreter can already have a
module imported -- a checkpoint is captured with some set of packages
preloaded (see ``preimport.py``) -- and evicting it from ``sys.modules`` here
to force a reimport was tried and reverted: a native extension (numpy, say)
reimported at a different version while another already-loaded module (e.g.
a preimported ``transformers``) still holds state built against the old one
means two ABI-incompatible copies of the same extension resident in one
process, which can crash the interpreter outright. ``server.py`` handles this
correctly instead, by running the call itself in a freshly spawned interpreter
whenever ``installed_versions()`` shows the install actually changed
something -- see ``readie_executor._invoke_subprocess``. A pin already
satisfied changes nothing on disk, and the call stays in this already-warm
process, so preimported packages keep paying off whenever a request's
``packages=`` was already sitting at the version the checkpoint preloaded.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
from importlib import metadata
from pathlib import Path

#: Where the rootfs bakes a working nameserver config (see pipeline/Dockerfile).
#: Not baked at /etc/resolv.conf directly -- Docker treats that exact path as a
#: runtime-managed file and drops anything written to it at build time.
_RESOLV_CONF_DEFAULT = Path("/etc/readie-resolv.conf")
_RESOLV_CONF = Path("/etc/resolv.conf")


class InstallError(Exception):
    """`uv pip install` exited non-zero."""


def _ensure_dns() -> None:
    """Copy in a working /etc/resolv.conf if the rootfs shipped an empty one.

    Nothing populates /etc/resolv.conf for a gVisor sandbox the way a normal
    container runtime would at container-create time, so without this every
    DNS lookup -- including the one a package install needs to reach PyPI --
    fails. Best-effort: an older rootfs built before _RESOLV_CONF_DEFAULT
    existed just leaves this a no-op rather than failing the call outright.
    """
    try:
        if _RESOLV_CONF.stat().st_size == 0 and _RESOLV_CONF_DEFAULT.exists():
            shutil.copyfile(_RESOLV_CONF_DEFAULT, _RESOLV_CONF)
    except OSError:
        pass


def install_packages(specs: list[str], *, uv_binary: str = "uv") -> str:
    """Install ``specs`` with ``uv``. A no-op for an empty list.

    Returns the combined stdout/stderr for logging. Raises ``InstallError``
    (with that same output) on a non-zero exit -- most commonly no network
    reaching PyPI, or a name/version that does not exist.
    """
    if not specs:
        return ""

    _ensure_dns()

    result = subprocess.run(  # noqa: S603 - specs are requirement strings, not shell input
        [uv_binary, "pip", "install", "--system", "--no-cache-dir", *specs],
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0:
        msg = f"uv pip install failed (exit {result.returncode}): {output.strip()}"
        raise InstallError(msg)
    return output


def installed_versions() -> dict[str, str]:
    """Every installed distribution's name -> version, right now.

    Lets a caller tell whether an ``install_packages`` call actually changed
    anything on disk, by comparing a snapshot from before the call against one
    from after.
    """
    importlib.invalidate_caches()
    return {
        dist.metadata["Name"]: dist.version
        for dist in metadata.distributions()
        if dist.metadata["Name"]
    }
