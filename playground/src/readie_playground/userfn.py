"""Build a module that runs source on Readie with session-scoped script semantics."""

from __future__ import annotations

import ast
import importlib.util
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import cloudpickle


@contextmanager
def _module(text: str, workdir: Path) -> Iterator[Any]:
    """Write ``text`` as a module file, import it, ship its functions by value, clean up."""
    workdir.mkdir(parents=True, exist_ok=True)
    name = f"pg_{uuid.uuid4().hex}"
    path = workdir / f"{name}.py"
    path.write_text(text, encoding="utf-8")
    module = None
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            msg = "could not load the generated module"
            raise ImportError(msg)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        # The sandbox cannot import this module by name, so ship its functions by value.
        cloudpickle.register_pickle_by_value(module)
        yield module
    finally:
        if module is not None:
            with suppress(ValueError):
                cloudpickle.unregister_pickle_by_value(module)
        sys.modules.pop(name, None)
        path.unlink(missing_ok=True)


_STEP_TEMPLATE = """\
import os
import sys
import types


def _run(source, key, index):
    os.environ.setdefault("MPLBACKEND", "Agg")
    name = "__readie_execution__"
    holder = sys.modules.setdefault(name, types.ModuleType(name))
    spaces = holder.__dict__.setdefault("spaces", {})
    namespace = spaces.setdefault(key, {"__name__": "__main__"})
    exec(compile(source, "<step %%d>" %% index, "exec"), namespace)


def main():
%(imports)s
    _run(%(source)s, %(key)s, %(index)d)
"""


def import_statements(source: str) -> list[str]:
    """The import statements in ``source``, as lines that are legal inside a function.

    ``from __future__`` and relative imports are skipped, and ``from x import *``
    becomes ``import x``, which is how the client records it anyway.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    found: dict[str, None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found[ast.unparse(node)] = None
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "__future__":
                continue
            if any(alias.name == "*" for alias in node.names):
                found[f"import {node.module}"] = None
            else:
                found[ast.unparse(node)] = None
    return list(found)


def build_step_module(source: str, key: str, index: int) -> str:
    """Module source whose ``main`` runs ``source`` as step ``index`` of session ``key``."""
    lines = []
    for statement in import_statements(source):
        lines += ["    try:", f"        {statement}", "    except Exception:", "        pass"]
    imports = "\n".join(lines) if lines else "    pass"
    return _STEP_TEMPLATE % {
        "imports": imports,
        "source": repr(source),
        "key": repr(key),
        "index": index,
    }


def check_step(source: str) -> str | None:
    """A readable error if ``source`` does not compile, else ``None``. Never runs it."""
    try:
        compile(source, "<step>", "exec", dont_inherit=True)
    except SyntaxError as exc:
        return f"SyntaxError: {exc.msg} (line {exc.lineno or 1})"
    except ValueError as exc:
        return f"ValueError: {exc}"
    return None


@contextmanager
def loaded_step(source: str, key: str, index: int, workdir: Path) -> Iterator[Callable[..., Any]]:
    """Write, import and yield ``main`` for one step; always clean up."""
    with _module(build_step_module(source, key, index), workdir) as module:
        yield module.main
