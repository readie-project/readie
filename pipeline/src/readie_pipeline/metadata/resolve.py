"""Resolve a raw dotted import string to the module it actually names.

``from x.y import z`` might name a real submodule (``PIL.Image`` is one) or
just an attribute of ``x.y`` (``sklearn.svm.LinearSVC`` is a class, not a
submodule) -- that can only be told apart by actually trying to import it,
which requires the target environment's packages to be installed.
``corpus/tree_parser.py`` records the deepest candidate implied by the syntax
(``x.y.z``); this module tries it, then each shorter prefix, and keeps
whichever one is real.
"""

from __future__ import annotations

import importlib
import json
import sys


def resolve_import(raw: str) -> str | None:
    """The deepest dotted prefix of ``raw`` that actually imports, or None.

    Tries ``raw`` itself first, then each shorter prefix (``x.y.z``, then
    ``x.y``, then ``x``), stopping at the first one that imports successfully.
    None if not even the first segment does -- the corpus recorded a package
    that was never installed, or a name that never existed.
    """
    segments = raw.split(".")
    for depth in range(len(segments), 0, -1):
        candidate = ".".join(segments[:depth])
        try:
            importlib.import_module(candidate)
        except BaseException:  # noqa: BLE001, S112 - a third-party module's own
            # init code can raise literally anything; a shorter prefix may
            # still resolve, so this must not stop the walk.
            continue
        return candidate
    return None


def discover(raw: str) -> tuple[str | None, frozenset[str]]:
    """Resolve ``raw`` and return its full empirical import closure.

    Must run in a fresh process: importing something records it in
    ``sys.modules`` for the life of the interpreter, so calling this a second
    time in the same process would diff against a baseline already grown by
    the first call. ``metadata/analyze.py`` always invokes this via
    ``python -m readie_pipeline.metadata.resolve``, one fresh subprocess per
    distinct raw string.
    """
    before = set(sys.modules)
    resolved = resolve_import(raw)
    if resolved is None:
        return None, frozenset()
    return resolved, frozenset(set(sys.modules) - before)


def _main() -> None:
    resolved, loaded = discover(sys.argv[1])
    print(json.dumps({"resolved": resolved, "loaded": sorted(loaded)}))


if __name__ == "__main__":
    _main()
