"""Startup imports."""

from __future__ import annotations

import sys

import pytest

from readie_executor.preimport import preimport


def test_real_modules_load():
    report = preimport(("json", "statistics"))
    assert report.loaded == ["json", "statistics"]
    assert report.failed == {}
    assert "json" in sys.modules


def test_a_missing_module_raises_so_the_pipeline_sees_a_failed_capture():
    # A bad name in a planned set means the whole capture is invalid.
    # __main__ calls preimport unguarded in capture mode, so this must crash
    # the process rather than ship a checkpoint that is silently missing a
    # module.
    with pytest.raises(ModuleNotFoundError):
        preimport(("json", "no_such_module_xyz", "statistics"))


def test_an_empty_set_is_fine():
    report = preimport(())
    assert report.loaded == []
    assert report.elapsed >= 0
