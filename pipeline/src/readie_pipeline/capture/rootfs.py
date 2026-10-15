"""What the rootfs a sandbox runs actually provides.

The catalogue carries over only the measured packages the rootfs has. The
inventory is taken by the rootfs's *own* ``python`` running
``metadata/inventory.py``, the same code ``analyze`` resolves distributions
with, so it reflects what that interpreter really imports from (its
``dist-packages`` directories, ``PYTHONPATH`` and all) and nothing here has to
guess where packages live. (``ROOTFS_PYTHONPATH`` names the right directory in
the Kaggle image now, but an earlier value pointed at one that did not exist,
and guessing it found nothing: the interpreter is asked instead.)

The interpreter is run in a ``chroot``, which the privileged capture container
allows. When it cannot be run or finds nothing, a message is printed and the
catalogue is simply not filtered.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from readie_pipeline.metadata import inventory as inventory_module
from readie_pipeline.metadata.inventory import Inventory

#: Runs a command and returns the finished process. Replaced in tests.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def discover_rootfs_inventory(
    rootfs: Path, pythonpath: str, *, run: Runner = subprocess.run
) -> Inventory | None:
    """List the distributions installed in the rootfs, or ``None`` if that fails.

    Runs ``inventory.py`` (fed on stdin, so nothing is copied into the rootfs)
    with the rootfs's ``python`` and the ``PYTHONPATH`` the sandbox is given.
    A failure, or an inventory with nothing in it, prints a message and
    returns ``None``: an empty inventory would make every measured package look
    absent, so it is treated as "unknown" rather than as an empty image.
    """
    source = Path(inventory_module.__file__).read_text()
    completed = run(
        ["chroot", str(rootfs), "env", f"PYTHONPATH={pythonpath}", "python", "-"],
        input=source,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        print(
            f"[!] could not list the packages in the rootfs at {rootfs} "
            f"({completed.stderr.strip()}); the catalogue will not be filtered"
        )
        return None

    try:
        inventory = Inventory.from_json(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        print(f"[!] unreadable package list from the rootfs ({exc}); catalogue not filtered")
        return None

    if not inventory.distributions:
        print(f"[!] the rootfs at {rootfs} reports no packages; catalogue not filtered")
        return None

    print(f"[*] the rootfs provides {len(inventory.distributions)} distributions")
    return inventory
