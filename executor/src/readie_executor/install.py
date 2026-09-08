"""Installing packages with ``uv`` before a call runs.

Every request restores a fresh, isolated container (warm reuse is currently
disabled worker-side), so there is nothing to check an "already installed"
package against -- this always shells out, even for a package the rootfs
already carries.

The restored interpreter can already have a module imported -- a checkpoint is
captured with some set of packages preloaded (see ``preimport.py``) -- so a
plain ``uv pip install`` only rewrites files on disk; ``sys.modules`` keeps
serving whatever was already imported at checkpoint-capture time regardless.
This module also evicts exactly the ``sys.modules`` entries for a distribution
whose version the install actually changed, so the next ``import`` inside the
call picks up what was just installed rather than the preloaded copy.
"""

from __future__ import annotations

import importlib
import re
import shutil
import subprocess
import sys
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
    before = _installed_versions()

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

    _drop_stale_modules(before)
    return output


def _normalize(name: str) -> str:
    """PEP 503 normalize ``name`` so casing and ``-``/``_``/``.`` don't matter."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _installed_versions() -> dict[str, str]:
    """Normalized distribution name -> installed version, right now."""
    return {
        _normalize(dist.metadata["Name"]): dist.version
        for dist in metadata.distributions()
        if dist.metadata["Name"]
    }


def _drop_stale_modules(before: dict[str, str]) -> None:
    """Evict ``sys.modules`` entries for any distribution ``uv`` just changed.

    Only distributions whose version actually moved are touched -- an install
    that finds its pin already satisfied changes nothing here either, so a
    call that does not need it pays no cost and loses no unrelated state.
    """
    importlib.invalidate_caches()
    after = _installed_versions()
    changed = {name for name, version in after.items() if before.get(name) != version}
    if not changed:
        return

    stale_modules = {
        module
        for module, dists in metadata.packages_distributions().items()
        if any(_normalize(dist) in changed for dist in dists)
    }
    for module_name in list(sys.modules):
        if module_name.partition(".")[0] in stale_modules:
            del sys.modules[module_name]
