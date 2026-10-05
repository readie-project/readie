"""Reconstruct submitted functions against a session-owned namespace."""

from __future__ import annotations

from collections.abc import Callable
from types import FunctionType
from typing import Any

# Module info globals not worth warning about. They are still discarded.
# The executor supplies its own minimal namespace of these variables.
MODULE_SCAFFOLDING = frozenset(
    {
        "__builtins__",
        "__name__",
        "__package__",
        "__loader__",
        "__spec__",
        "__file__",
        "__cached__",
        "__doc__",
        "__path__",
    }
)


def bind_session_globals(
    func: Callable[..., Any], session_globals: dict[str, Any]
) -> tuple[FunctionType, list[dict[str, Any]]]:
    """Replace a function's globals without modifying its original module or captures."""
    if not isinstance(func, FunctionType):
        msg = f"session globals support plain Python functions only; received {type(func).__name__}"
        raise TypeError(msg)

    ignored = [str(key) for key in func.__globals__ if key not in MODULE_SCAFFOLDING]
    warnings: list[dict[str, Any]] = []
    if ignored:
        message = (
            f"{len(ignored)} client globals were ignored for this session call. "
            "Initialize globals inside a remote session function or pass values explicitly. "
        )
        warnings.append({"code": "ignored_globals", "message": message, "names": ignored})

    bound = FunctionType(
        func.__code__, session_globals, func.__name__, func.__defaults__, func.__closure__
    )
    bound.__kwdefaults__ = func.__kwdefaults__
    bound.__annotations__ = func.__annotations__
    bound.__type_params__ = func.__type_params__
    bound.__qualname__ = func.__qualname__
    bound.__module__ = func.__module__
    bound.__doc__ = func.__doc__
    bound.__dict__.update(func.__dict__)
    return bound, warnings
