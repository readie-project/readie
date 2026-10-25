"""Emptying the kernel's file cache, so an import is timed the way a fresh worker meets it.

A fresh process or sandbox does not start with an empty file cache. The cache lives in the
host kernel, so a library file read once stays in memory for every later process until
something evicts it. The ``analyze`` stage reads a package during discovery before it times
the import, which makes the timed import a warm one. Emptying the whole cache just before a
timed run needs no list of the files a package loads, so it cannot miss one.
"""

from __future__ import annotations

import ctypes
import mmap
import os
import tempfile
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from readie_pipeline.config import ConfigError

DROP_CACHES = Path("/proc/sys/vm/drop_caches")

#: Size of the file used to confirm a drop. Small enough to read in a moment, large enough
#: that a few pages left behind by other activity do not look like a failed drop.
_CANARY_BYTES = 32 * 1024 * 1024

#: Share of the confirmation file that may still be resident after a drop.
_MAX_RESIDENT = 0.1


class CacheError(ConfigError):
    """The file cache could not be emptied, or did not empty."""


class CacheDropper(Protocol):
    """Empties the file cache. A seam, so tests and other hosts can substitute their own."""

    def drop(self) -> None:
        """Return once the cache is empty, or raise ``CacheError``."""
        ...


def resident_fraction(path: Path) -> float:
    """The share of ``path`` that is in the file cache right now, asked of the kernel.

    Uses ``mincore`` on a mapping of the file, so it reports pages that are resident
    without bringing any in.
    """
    size = path.stat().st_size
    if size == 0:
        return 0.0
    pages = (size + mmap.PAGESIZE - 1) // mmap.PAGESIZE
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte)]
    vector = (ctypes.c_ubyte * pages)()
    with path.open("rb") as handle, mmap.mmap(handle.fileno(), size, access=mmap.ACCESS_COPY) as m:
        # A writable view is needed to take the address; nothing is written through it.
        view = ctypes.c_char.from_buffer(m)
        status = libc.mincore(ctypes.addressof(view), size, vector)
        del view
    if status != 0:
        msg = f"mincore failed on {path}: {os.strerror(ctypes.get_errno())}"
        raise CacheError(msg)
    return sum(byte & 1 for byte in vector) / pages


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)


class KernelCacheDropper:
    """Empties the Linux page, dentry and inode caches through ``drop_caches``.

    This acts on the whole host, not on one process, so run it on a machine where that is
    acceptable. It needs write access to the file, which means root; inside a container,
    mount the host's file in (the Makefile's ``analyze`` target does).

    Each ``drop`` confirms that it worked. Before the drop it reads a file of its own, so
    that file is in the cache; after the drop it asks the kernel how much of that file is
    still resident, and raises when most of it is. A drop that silently did nothing would
    leave the timings warm and give no sign of it. The size of the whole cache is no help
    here: some of it can never be dropped, because running processes have those files
    mapped, and how much differs from host to host.

    Files that other running processes have mapped stay resident too. Run the analysis on a
    host where nothing else is using the packages being timed.
    """

    def __init__(
        self,
        drop_file: Path = DROP_CACHES,
        sync: Callable[[], None] = os.sync,
        canary_dir: Path | None = None,
        probe: Callable[[Path], float] = resident_fraction,
    ) -> None:
        self._drop_file = drop_file
        self._sync = sync
        self._canary_dir = canary_dir
        self._probe = probe
        self._canary: Path | None = None

    def check(self) -> None:
        """Fail now if ``drop`` cannot work, rather than partway through a long run."""
        if not os.access(self._drop_file, os.W_OK):
            msg = (
                f"cannot write {self._drop_file}, so the file cache cannot be emptied. "
                "Run as root on a host where dropping the whole file cache is acceptable. "
                "In a container, root alone is not enough, because /proc/sys is read-only "
                "there: mount the host's file into it (see the analyze target in the "
                "Makefile) and point READIE_DROP_CACHES_FILE at it."
            )
            raise CacheError(msg)
        self._canary_path()

    def drop(self) -> None:
        """Flush dirty pages, empty the caches, and confirm that they emptied."""
        canary = self._canary_path()
        self._sync()
        canary.read_bytes()
        try:
            self._drop_file.write_text("3\n")
        except OSError as exc:
            msg = f"could not write to {self._drop_file}: {exc}"
            raise CacheError(msg) from exc
        remaining = self._probe(canary)
        if remaining > _MAX_RESIDENT:
            msg = (
                f"emptying the file cache left {remaining:.0%} of a file that had just been "
                "read in memory, so the next timing would not be cold"
            )
            raise CacheError(msg)

    def _canary_path(self) -> Path:
        """The confirmation file, created on first use and removed when this object goes."""
        if self._canary is None:
            try:
                descriptor, name = tempfile.mkstemp(prefix=".readie-canary-", dir=self._canary_dir)
            except OSError as exc:
                msg = f"cannot create the file that confirms a drop: {exc}"
                raise CacheError(msg) from exc
            path = Path(name)
            weakref.finalize(self, _remove, path)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(os.urandom(1024 * 1024) * (_CANARY_BYTES // (1024 * 1024)))
                handle.flush()
                os.fsync(handle.fileno())
            self._canary = path
        return self._canary
