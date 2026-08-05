"""Static import extraction.

The client walks a function's source for the modules it refers to and forwards
them to the router, which passes them to the worker so a checkpoint that already
imported those modules can be chosen. This is *checkpoint selection*, not memory
sizing -- what a call needs in memory is now declared on the decorator, not
guessed from its source.

Extraction is deferred to the first call and cached per function, so importing a
module full of ``@remote`` definitions stays free while a repeatedly called
function pays for analysis once. A source that cannot be read or parsed yields no
imports rather than an error: a decorator defined in a REPL, an ``exec`` or a
frozen binary has no retrievable source, and none of that should fail a call.
"""

from __future__ import annotations

import inspect
import textwrap
from collections.abc import Callable
from typing import Any

from crfs import _ast

_cache: dict[int, tuple[str, ...]] = {}


def extract_imports(func: Callable[..., Any]) -> tuple[str, ...]:
    """Return the top-level modules ``func`` refers to, sorted and deduped.

    Models and tokenizers named in ``from_pretrained(...)`` calls ride along as
    ``model:<name>`` entries -- they are artefacts a checkpoint might stage, and
    the wire carries imports as plain strings.
    """
    key = id(func)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    imports = _analyse(func)
    _cache[key] = imports
    return imports


def _analyse(func: Callable[..., Any]) -> tuple[str, ...]:
    source = _source_of(func)
    if source is None:
        return ()
    try:
        facts = _ast.analyse(source)
    except SyntaxError:
        # Only reachable when dedenting a fragment produced something the parser
        # rejects. Import hints are advisory; a hint is not worth a crash.
        return ()
    modules = sorted(facts.imports)
    artefacts = sorted(f"model:{name}" for name in facts.models | facts.tokenizers)
    return tuple(modules + artefacts)


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
