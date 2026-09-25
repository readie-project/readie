"""Resolving a raw dotted import string to the module it actually names.

Exercised against the standard library, which is always importable
regardless of environment, rather than against fakes -- there is nothing to
monkeypatch here: ``resolve_import`` and ``discover`` really do call
``importlib.import_module``.
"""

from __future__ import annotations

import json
import subprocess
import sys

from readie_pipeline.metadata.resolve import discover, resolve_import


def test_a_real_module_resolves_to_itself():
    assert resolve_import("os.path") == "os.path"


def test_a_class_or_function_falls_back_to_its_owning_module():
    # xml.etree.ElementTree.parse is a function, not a module.
    assert resolve_import("xml.etree.ElementTree.parse") == "xml.etree.ElementTree"


def test_a_completely_unknown_name_resolves_to_nothing():
    assert resolve_import("this_package_does_not_exist_anywhere") is None


def test_a_deeper_invalid_segment_still_falls_back_correctly():
    # `os.path.join.nested` -- `join` is a function, so nothing past `os.path`
    # is ever real, however many more segments are appended.
    assert resolve_import("os.path.join.nested") == "os.path"


def test_discover_in_process_still_resolves_even_if_the_closure_is_stale():
    # `os` is essentially always already imported by the time any test runs
    # (pytest itself pulls it in), so a `before`/`after` sys.modules diff taken
    # in this same process can legitimately be empty even though resolution
    # succeeds -- this is exactly why analyze.py always calls `discover` in a
    # fresh subprocess (see the test below for that case).
    resolved, _loaded = discover("os.path")
    assert resolved == "os.path"


def test_discover_of_an_unresolvable_name_is_empty():
    resolved, loaded = discover("this_package_does_not_exist_anywhere")
    assert resolved is None
    assert loaded == frozenset()


def test_discover_in_a_fresh_subprocess_reports_a_real_closure():
    # This is how analyze.py actually invokes it: a clean interpreter, so the
    # sys.modules diff is not polluted by whatever the test process already
    # happens to have imported.
    completed = subprocess.run(
        [sys.executable, "-m", "readie_pipeline.metadata.resolve", "xml.dom.minidom"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    payload = json.loads(completed.stdout.strip())

    assert payload["resolved"] == "xml.dom.minidom"
    assert "xml.dom.minidom" in payload["loaded"]
