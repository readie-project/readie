"""Startup imports and the ready sentinel."""

from __future__ import annotations

import sys

from crfs_executor.config import READY_SENTINEL
from crfs_executor.preimport import announce_ready, await_checkpoint, preimport


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


def test_the_sentinel_is_printed_verbatim(capsys):
    # The pipeline greps for exactly this string; nothing else is a contract.
    announce_ready()
    assert READY_SENTINEL in capsys.readouterr().out


def test_the_summary_precedes_the_sentinel(capsys):
    announce_ready(preimport(("json",)))
    lines = [line for line in capsys.readouterr().out.splitlines() if line]

    assert lines[-1] == READY_SENTINEL
    assert "1 loaded" in lines[0]


def test_a_zero_sleep_returns_immediately():
    await_checkpoint(0)
