"""Serialisation of the call and its result.

## Trust boundary

``CloudpickleCodec.decode_result`` unpickles bytes that arrived over the wire,
and unpickling executes arbitrary code by design. A compromised router or worker
can therefore run code in the *client's* process. This is inherent to shipping
live Python function objects and cannot be fixed by validating the payload.

It is confined here rather than papered over: ``ResultCodec`` is the seam, so a
deployment that cannot accept that boundary can supply a codec restricted to a
safe format and lose only the ability to return arbitrary objects.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

import sys
import cloudpickle

from readie.errors import SerializationError


@runtime_checkable
class ResultCodec(Protocol):
    """Converts a call to bytes and bytes back to a result."""

    def encode_call(
        self,
        func: Callable[..., Any],
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
        packages: Sequence[str] = (),
    ) -> bytes:
        """Serialise a call for the executor."""
        ...

    def decode_result(self, raw: bytes) -> Any:
        """Deserialise what the executor sent back."""
        ...


class CloudpickleCodec:
    """The wire format the executor already speaks.

    The executor unpickles a mapping and calls
    ``data['func'](*data['args'], **data['kwargs'])``. The three keys and their
    spelling are the contract; changing them requires changing the executor.
    ``packages`` is a fourth, optional key: PyPI requirement specs the executor
    installs with ``uv`` before invoking ``func``, so a caller's requested
    packages travel with the call itself rather than through a separate
    channel router and worker would otherwise have to know about.
    """

    def encode_call(
        self,
        func: Callable[..., Any],
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
        packages: Sequence[str] = (),
    ) -> bytes:
        """Pickle the function object together with its arguments."""

        # Print the Python interpreter version
        print(f"Python version: {sys.version}")
        # Print the cloudpickle package version
        print(f"cloudpickle version: {cloudpickle.__version__}")

        try:
            return bytes(
                cloudpickle.dumps(
                    {
                        "func": func,
                        "args": tuple(args),
                        "kwargs": dict(kwargs),
                        "packages": list(packages),
                    }
                )
            )
        except Exception as exc:
            name = getattr(func, "__qualname__", repr(func))
            msg = f"cannot serialise call to {name}: {exc}"
            raise SerializationError(msg) from exc

    def decode_result(self, raw: bytes) -> Any:
        """Unpickle the executor's return value. See the module trust note."""
        try:
            return cloudpickle.loads(raw)
        except Exception as exc:
            msg = f"cannot deserialise the result ({len(raw)} bytes): {exc}"
            raise SerializationError(msg) from exc
