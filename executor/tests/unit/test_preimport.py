"""Startup imports."""

from __future__ import annotations

import sys

from crfs_executor.preimport import preimport


def test_real_modules_load():
    report = preimport(("json", "statistics"))
    assert report.loaded == ["json", "statistics"]
    assert report.failed == {}
    assert "json" in sys.modules


def test_a_missing_module_is_recorded_and_the_rest_still_load():
    # One bad name in a planned set must leave a checkpoint missing a module,
    # not a sandbox that exited. The pipeline can only tell those apart if this
    # keeps going.
    report = preimport(("json", "no_such_module_xyz", "statistics"))

    assert report.loaded == ["json", "statistics"]
    assert "no_such_module_xyz" in report.failed
    assert "ModuleNotFoundError" in report.failed["no_such_module_xyz"]


def test_an_empty_set_is_fine():
    report = preimport(())
    assert report.loaded == []
    assert report.elapsed >= 0
