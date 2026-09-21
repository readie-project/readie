"""Installing packages with ``uv`` before a call runs.

Every request restores a fresh, isolated container (warm reuse is currently
disabled worker-side), so there is nothing to check an "already installed"
package against -- this always shells out, even for a package the rootfs
already carries.
"""

from __future__ import annotations

import subprocess


class InstallError(Exception):
    """`uv pip install` exited non-zero."""


def install_packages(specs: list[str], *, uv_binary: str = "uv") -> str:
    """Install ``specs`` with ``uv``. A no-op for an empty list.

    Returns the combined stdout/stderr for logging. Raises ``InstallError``
    (with that same output) on a non-zero exit -- most commonly no network
    reaching PyPI, or a name/version that does not exist.
    """
    if not specs:
        return ""

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
