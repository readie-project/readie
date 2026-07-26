"""Resource estimation.

The router forwards an estimate to the worker so a checkpoint with the right
packages can be chosen. The previous client sent a fixed
``ResourceEstimation(code="", variables=[<one empty Variables>], imports=[...])``
-- a placeholder that was, in any case, dropped by the router and never reached
a worker.

``ResourceEstimator`` is a ``Protocol`` over plain dataclasses, deliberately: the
real prediction model lives in a different repository and must be able to satisfy
this seam without importing ``crfs`` or protobuf.
"""

from __future__ import annotations

import inspect
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from crfs import _ast


@dataclass(frozen=True, slots=True)
class Import:
    """A module the function refers to."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Variable:
    """A value the function binds, as far as static analysis can tell."""

    id: str
    value: str = ""
    type: str = ""
    shape: str = ""
    ctx: str | None = None


@dataclass(frozen=True, slots=True)
class Estimate:
    """What a scheduler is told about a call before it runs."""

    code: str = ""
    imports: tuple[Import, ...] = field(default_factory=tuple)
    variables: tuple[Variable, ...] = field(default_factory=tuple)


EMPTY_ESTIMATE = Estimate()


@runtime_checkable
class ResourceEstimator(Protocol):
    """Predicts what a call will need."""

    def estimate(self, func: Callable[..., Any]) -> Estimate:
        """Describe the resources ``func`` is likely to require."""
        ...


class NullEstimator:
    """Sends nothing. Useful when analysis is unwanted or too slow."""

    def estimate(self, func: Callable[..., Any]) -> Estimate:  # noqa: ARG002
        """Return an empty estimate."""
        return EMPTY_ESTIMATE


class AstEstimator:
    """Derives an estimate from the function's own source.

    Estimation is deferred to the first call and then cached per function, so
    importing a module full of ``@remote`` definitions stays free while a
    repeatedly called function pays for analysis once.

    Sources that cannot be read or parsed produce an empty estimate rather than
    an error: a decorator defined in a REPL, in an ``exec``, or in a frozen
    binary has no retrievable source, and none of that should fail a call.
    """

    def __init__(self, *, include_source: bool = True) -> None:
        self._include_source = include_source
        self._cache: dict[int, Estimate] = {}

    def estimate(self, func: Callable[..., Any]) -> Estimate:
        """Analyse ``func``'s source, or return an empty estimate."""
        key = id(func)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        estimate = self._analyse(func)
        self._cache[key] = estimate
        return estimate

    def _analyse(self, func: Callable[..., Any]) -> Estimate:
        source = _source_of(func)
        if source is None:
            return EMPTY_ESTIMATE
        try:
            facts = _ast.analyse(source)
        except SyntaxError:
            # Only reachable when dedenting a fragment produced something the
            # parser rejects. An estimate is a hint; a hint is not worth a crash.
            return EMPTY_ESTIMATE

        imports = tuple(Import(id=name, name=name) for name in sorted(facts.imports))
        variables = tuple(
            Variable(id=name, type=type_name)
            for name, type_name in sorted(facts.assignments.items())
        )
        models = tuple(
            # Models and tokenizers are artefacts to stage, not modules; the
            # proto has no field for them, so they ride along as imports with a
            # distinguishing id.
            Import(id=f"model:{name}", name=name)
            for name in sorted(facts.models | facts.tokenizers)
        )
        return Estimate(
            code=source if self._include_source else "",
            imports=imports + models,
            variables=variables,
        )


def _source_of(func: Callable[..., Any]) -> str | None:
    """Return ``func``'s dedented source, or ``None`` if it is unavailable."""
    target = inspect.unwrap(func)
    try:
        source = inspect.getsource(target)
    except (OSError, TypeError):
        # OSError: no source file (REPL, exec, frozen). TypeError: a builtin.
        return None
    # A decorator line referring to a name the parser cannot resolve is fine --
    # this is syntax only -- and dedent handles a method's leading indentation.
    return textwrap.dedent(source)
