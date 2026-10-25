"""Emptying the file cache, and knowing that it was emptied."""

from __future__ import annotations

import gc
from collections.abc import Callable
from pathlib import Path

import pytest

from readie_pipeline.metadata.cache import CacheError, KernelCacheDropper, resident_fraction

MIB = 1024 * 1024


def make_dropper(
    tmp_path: Path,
    *,
    sync: Callable[[], None] = lambda: None,
    probe: Callable[[Path], float] = lambda _path: 0.0,
) -> KernelCacheDropper:
    """A dropper that writes into ``tmp_path``, with the kernel's answer replaced by ``probe``."""
    return KernelCacheDropper(
        drop_file=tmp_path / "drop_caches", sync=sync, canary_dir=tmp_path, probe=probe
    )


def test_a_drop_syncs_first_and_then_writes_3(tmp_path):
    # Dirty pages cannot be dropped, so they have to reach the disk before the write.
    drop_file = tmp_path / "drop_caches"
    written_when_synced: list[bool] = []
    d = make_dropper(tmp_path, sync=lambda: written_when_synced.append(drop_file.exists()))

    d.drop()

    assert written_when_synced == [False], "the sync has to run before the write"
    assert drop_file.read_text() == "3\n"


def test_a_drop_is_confirmed_with_a_file_of_its_own(tmp_path):
    # The size of the whole cache cannot confirm a drop: running processes keep some of it
    # resident whatever is written. A file read in just before the drop can.
    probed: list[Path] = []

    sizes: list[int] = []

    def probe(path: Path) -> float:
        probed.append(path)
        sizes.append(path.stat().st_size)
        return 0.0

    make_dropper(tmp_path, probe=probe).drop()

    assert len(probed) == 1
    assert probed[0].parent == tmp_path
    assert sizes == [32 * MIB]


def test_a_drop_that_leaves_most_of_the_confirmation_file_in_memory_is_an_error(tmp_path):
    # A drop that silently did nothing would leave every timing warm, with no sign of it.
    d = make_dropper(tmp_path, probe=lambda _path: 0.9)
    with pytest.raises(CacheError, match="would not be cold"):
        d.drop()


def test_a_few_pages_left_in_memory_are_fine(tmp_path):
    # Other activity on the host can touch a handful of the file's pages meanwhile.
    make_dropper(tmp_path, probe=lambda _path: 0.05).drop()


def test_a_drop_file_that_cannot_be_written_is_an_error(tmp_path):
    d = KernelCacheDropper(
        drop_file=tmp_path / "no-such-dir" / "drop_caches",
        sync=lambda: None,
        canary_dir=tmp_path,
        probe=lambda _path: 0.0,
    )
    with pytest.raises(CacheError, match="could not write"):
        d.drop()


def test_check_fails_up_front_when_the_drop_file_is_not_writable(tmp_path):
    # Root inside a container is not enough: /proc/sys is read-only there. The message says
    # so, because that is the cause people hit.
    with pytest.raises(CacheError, match="read-only"):
        make_dropper(tmp_path).check()


def test_check_passes_and_prepares_the_confirmation_file(tmp_path):
    (tmp_path / "drop_caches").write_text("")
    d = make_dropper(tmp_path)  # the file lives as long as the dropper does
    d.check()
    canaries = [p for p in tmp_path.iterdir() if p.name.startswith(".readie-canary-")]
    assert len(canaries) == 1


def test_check_fails_when_the_confirmation_file_cannot_be_created(tmp_path):
    (tmp_path / "drop_caches").write_text("")
    d = KernelCacheDropper(
        drop_file=tmp_path / "drop_caches", canary_dir=tmp_path / "missing", sync=lambda: None
    )
    with pytest.raises(CacheError, match="cannot create"):
        d.check()


def test_the_confirmation_file_goes_away_with_the_dropper(tmp_path):
    (tmp_path / "drop_caches").write_text("")
    d = make_dropper(tmp_path)
    d.check()
    canary = next(p for p in tmp_path.iterdir() if p.name.startswith(".readie-canary-"))

    del d
    gc.collect()

    assert not canary.exists()


def test_a_file_that_was_just_read_is_resident(tmp_path):
    # The one test that asks the real kernel. A file that was just written and read back is
    # in the page cache, on a disk filesystem and in memory alike.
    path = tmp_path / "data"
    path.write_bytes(b"x" * (4 * MIB))
    path.read_bytes()
    assert resident_fraction(path) > 0.9


def test_an_empty_file_has_nothing_resident(tmp_path):
    path = tmp_path / "empty"
    path.write_bytes(b"")
    assert resident_fraction(path) == 0.0
