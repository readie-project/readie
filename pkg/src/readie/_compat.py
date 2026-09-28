"""Interpreter compatibility with the sandbox executor."""

from __future__ import annotations

import sys

from readie.errors import IncompatiblePythonError

# Must match the executor's interpreter and the ``requires-python`` floor.
REQUIRED_PYTHON: tuple[int, int] = (3, 12)


def check_python_version(version: tuple[int, ...] | None = None) -> None:
    """Raise ``IncompatiblePythonError`` unless ``version`` is the required minor."""
    major, minor = (version or tuple(sys.version_info[:2]))[:2]
    if (major, minor) != REQUIRED_PYTHON:
        msg = (
            f"readie requires Python {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]} to match the "
            f"remote executor, but this is Python {major}.{minor}"
        )
        raise IncompatiblePythonError(msg)
