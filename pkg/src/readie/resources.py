"""Static import extraction.

The client walks a function's source for the modules it refers to and forwards
them to the router, which passes them to the worker so a checkpoint that already
imported those modules can be chosen. This is *checkpoint selection*, not memory
sizing -- what a call needs in memory is now declared on the decorator, not
guessed from its source.

A module imported outside the function but merely referenced inside it (a
top-of-file ``import numpy as np`` used as ``np.dot(...)`` in the body) never
shows up as an ``import`` statement in the function's own source, so it is
resolved separately by looking up the function's free variables -- its globals
and closure cells -- against the modules they actually came from.

Extraction is deferred to the first call and cached per function, so importing a
module full of ``@remote`` definitions stays free while a repeatedly called
function pays for analysis once. A source that cannot be read or parsed yields no
imports rather than an error: a decorator defined in a REPL, an ``exec`` or a
frozen binary has no retrievable source, and none of that should fail a call.
"""

from __future__ import annotations

import contextlib
import inspect
import textwrap
import types
import weakref
from collections.abc import Callable
from typing import Any

from readie import _ast

# Keyed by the function object itself, not id(func): a cache keyed by id alone
# is unsafe once the original function is garbage collected, since CPython can
# and does hand that same id to an unrelated function defined later, producing
# a false cache hit for entirely different source. A builtin (e.g. len) is
# neither hashable in a useful way here nor weakly referenceable, so both
# lookup and store fall back to no caching for it -- extraction is cheap for
# something with no retrievable source anyway.
_cache: weakref.WeakKeyDictionary[Callable[..., Any], tuple[str, ...]] = weakref.WeakKeyDictionary()


def extract_imports(func: Callable[..., Any]) -> tuple[str, ...]:
    """Return the top-level modules ``func`` refers to, sorted and deduped.

    Models and tokenizers named in ``from_pretrained(...)`` calls ride along as
    ``model:<name>`` entries -- they are artefacts a checkpoint might stage, and
    the wire carries imports as plain strings.
    """
    try:
        cached = _cache.get(func)
    except TypeError:
        cached = None
    if cached is not None:
        return cached
    imports = _analyse(func)
    with contextlib.suppress(TypeError):
        _cache[func] = imports
    return imports


def _analyse(func: Callable[..., Any]) -> tuple[str, ...]:
    target = inspect.unwrap(func)
    source = _source_of(target)
    if source is None:
        return ()
    try:
        facts = _ast.analyse(source)
    except SyntaxError:
        # Only reachable when dedenting a fragment produced something the parser
        # rejects. Import hints are advisory; a hint is not worth a crash.
        return ()
    modules = sorted(facts.imports | _external_modules(target))
    artefacts = sorted(f"model:{name}" for name in facts.models | facts.tokenizers)
    return tuple(modules + artefacts)


def _source_of(target: Callable[..., Any]) -> str | None:
    """Return ``target``'s dedented source, or ``None`` if it is unavailable."""
    try:
        source = inspect.getsource(target)
    except (OSError, TypeError):
        # OSError: no source file (REPL, exec, frozen). TypeError: a builtin.
        return None
    # A decorator line referring to a name the parser cannot resolve is fine --
    # this is syntax only -- and dedent handles a method's leading indentation.
    return textwrap.dedent(source)


def _external_modules(target: Callable[..., Any]) -> set[str]:
    """Return modules behind names ``target`` reaches as a global or closure cell.

    Covers ``import numpy as np`` at module scope with ``np.dot(...)`` used in
    the body: ``np`` is never a local import statement, but it is a free
    variable of ``target``, and ``inspect.getclosurevars`` resolves it to the
    real ``numpy`` module object without re-deriving Python's own scoping rules.
    """
    try:
        closure = inspect.getclosurevars(target)
    except TypeError:
        # Not a plain Python function/method (e.g. a builtin) -- nothing to walk.
        return set()
    own_module = getattr(target, "__module__", None)
    modules: set[str] = set()
    for value in (*closure.nonlocals.values(), *closure.globals.values()):
        name = _module_name_of(value)
        if name and name not in (own_module, "builtins"):
            modules.add(name)
    return modules


def _module_name_of(value: Any) -> str | None:
    """Name the module ``value`` came from, resolving through its type if needed."""
    if isinstance(value, types.ModuleType):
        return value.__name__
    return getattr(value, "__module__", None) or type(value).__module__
