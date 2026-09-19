"""The cloudpickle boundary.

Mirrors ``pkg/src/readie/codec.py`` on the client side; the two must agree on the
call shape or nothing runs. That shape is::

    {"func": <callable>, "args": <tuple>, "kwargs": <dict>, "packages": [<str>, ...]}

``packages`` is optional and defaults to empty: an older client that never
sends it still decodes, since it names PyPI requirements to install with
``uv`` before invoking ``func`` rather than anything the call itself needs.

# Trust

``decode_call`` unpickles bytes the worker handed it, and unpickling executes
arbitrary code. That is the executor's whole job - it exists to run code
somebody else wrote - so this is not a boundary to defend, it is the reason the
sandbox exists. Containment is gVisor, no network, and a read-only shared
rootfs. See SECURITY.md.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import cloudpickle


class DecodeError(Exception):
    """The request body was not a call this executor can make."""


@dataclass(frozen=True, slots=True)
class Call:
    """A decoded request."""

    func: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    packages: tuple[str, ...] = ()

    def invoke(self) -> Any:
        """Call the function. Any exception it raises propagates."""
        return self.func(*self.args, **self.kwargs)


def decode_call(raw: bytes) -> Call:
    """Unpickle a request body into a callable and its arguments.

    Validates the shape rather than indexing blindly: the previous
    implementation did ``data['func'](*data['args'], **data['kwargs'])``, so a
    body that unpickled to anything else raised ``KeyError`` or ``TypeError``
    inside the *user's* error path and was reported as the user's fault.
    """
    # Print the Python interpreter version
    print(f"[executor] Python version: {sys.version}")
    # Print the cloudpickle package version
    print(f"[executor] cloudpickle version: {cloudpickle.__version__}")

    try:
        payload = cloudpickle.loads(raw)
    except Exception as exc:
        msg = f"could not unpickle a {len(raw)}-byte request: {exc}"
        raise DecodeError(msg) from exc

    if not isinstance(payload, dict):
        msg = f"expected a mapping with func/args/kwargs, got {type(payload).__name__}"
        raise DecodeError(msg)

    missing = {"func", "args", "kwargs"} - payload.keys()
    if missing:
        msg = f"request is missing {', '.join(sorted(missing))}"
        raise DecodeError(msg)

    func = payload["func"]
    if not callable(func):
        msg = f"request 'func' is not callable, it is {type(func).__name__}"
        raise DecodeError(msg)

    args, kwargs = tuple(payload["args"]), dict(payload["kwargs"])
    packages = tuple(payload.get("packages", ()))
    print(
        f"[executor] decoded call: func={func}, args={args}, kwargs={kwargs}, packages={packages}",
        flush=True,
    )
    return Call(func=func, args=args, kwargs=kwargs, packages=packages)


def encode_result(value: Any) -> bytes:
    """Pickle a return value for the worker to relay."""
    return bytes(cloudpickle.dumps(value))
