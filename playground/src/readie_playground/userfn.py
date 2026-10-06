"""Turn a visitor's script into a ``@remote`` function, the way a user would write one.

The script becomes the body of ``main`` in a real module file. A real file means
``inspect.getsource`` works, so the client reads the script's imports and the
router can pick a matching checkpoint. Importing the module runs only the
decorator and the ``def``; the script itself runs on Readie.

This service runs under gVisor on a network that reaches only nginx. See
``SECURITY.md``.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import cloudpickle

from readie_playground.config import MEMORY

_TEMPLATE = """\
from readie import remote


@remote(memory="{memory}", max_memory="{memory}")
def main():
{body}
"""


def build_module(code: str) -> str:
    """Return module source with ``code`` as the body of ``main``."""
    body = textwrap.indent(code.rstrip() or "pass", "    ")
    return _TEMPLATE.format(memory=MEMORY, body=body)


def check(code: str) -> str | None:
    """Return a readable error if ``code`` does not compile, else ``None``.

    Compiling never runs the code. A visitor sees the same message Python would give.
    """
    try:
        compile(build_module(code), "<playground>", "exec")
    except SyntaxError as exc:
        # The generated module adds four lines of header; report the visitor's own line.
        line = max((exc.lineno or 5) - 5, 1)
        return f"SyntaxError: {exc.msg} (line {line})"
    return None


@contextmanager
def loaded(code: str, workdir: Path) -> Iterator[Callable[..., Any]]:
    """Write, import and yield the undecorated ``main``; always clean up."""
    workdir.mkdir(parents=True, exist_ok=True)
    name = f"pg_{uuid.uuid4().hex}"
    path = workdir / f"{name}.py"
    path.write_text(build_module(code), encoding="utf-8")
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
        yield module.main.func
    finally:
        if module is not None:
            with suppress(ValueError):
                cloudpickle.unregister_pickle_by_value(module)
        sys.modules.pop(name, None)
        path.unlink(missing_ok=True)
