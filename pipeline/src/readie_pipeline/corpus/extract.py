"""Derive an item's resources from its code.

Resources are read from the code itself, never taken from the model's say-so:
what the code imports and loads is what it needs. Notebook magics and shell
lines are removed first so the cells parse as Python.

KEEP THIS MODULE'S IMPORT EXTRACTION IN SYNC WITH ``pkg/src/readie/_ast.py``.
A live request's ``ExecutionConfig.imports`` comes from the SDK's visitor, and
the router matches it against a catalogue built from what this module records,
using plain string equality. If the two record a dotted import differently
(one keeping ``x.y.z`` and the other truncating to ``x.y``), a live request
stops matching catalogue entries it should, with no error raised anywhere. For
the same reason the standard library is not filtered out here: the SDK does not
filter it either.
"""

from __future__ import annotations

import ast
import re
import warnings
from collections.abc import Iterable

from readie_pipeline.corpus.models import ResourceKind, resource


class CodeError(Exception):
    """A code string is not valid Python."""


#: ``load_dataset`` builder names that are loaders, not Hub repositories.
_DATASET_BUILDERS = frozenset(
    {"csv", "json", "parquet", "text", "imagefolder", "audiofolder", "arrow", "pandas", "sql"}
)
_HUB_ID = re.compile(r"^[A-Za-z0-9][\w.-]*(/[\w.-]+)?$")
_KAGGLE_DATASET_ID = re.compile(r"^[\w.-]+/[\w.-]+$")


def clean_cell(source: str) -> str:
    """Drop IPython magics and shell escapes, keeping every other line."""
    kept = [line for line in source.splitlines() if not line.lstrip().startswith(("!", "%"))]
    return "\n".join(kept)


def parse(code: str) -> ast.Module:
    """Parse one code string, raising :class:`CodeError` when it is not Python."""
    try:
        with warnings.catch_warnings():
            # Fetched code is full of "\\d"-style strings that Python flags with a
            # SyntaxWarning while compiling; that says nothing about whether the
            # code is usable, and it would flood the progress output.
            warnings.simplefilter("ignore", SyntaxWarning)
            return ast.parse(clean_cell(code))
    except (SyntaxError, ValueError) as exc:
        msg = f"code does not parse: {exc}"
        raise CodeError(msg) from exc


def extract_resources(code: Iterable[str]) -> tuple[str, ...]:
    """Every resource the code strings use, sorted and de-duplicated."""
    found: set[str] = set()
    for cell in code:
        tree = parse(cell)
        found.update(_imports(tree))
        found.update(_hub_calls(tree))
    return tuple(sorted(found))


def _imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _add_package(found, alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            # ``from sklearn.svm import LinearSVC`` is the candidate
            # ``sklearn.svm.LinearSVC``; whether that names a module or an
            # attribute is for the metadata resolver to decide.
            for alias in node.names:
                _add_package(
                    found, node.module if alias.name == "*" else f"{node.module}.{alias.name}"
                )
    return found


def _add_package(found: set[str], name: str) -> None:
    found.add(resource(ResourceKind.PACKAGE, name))


def _hub_calls(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        arg = _first_string(node)
        if arg is None:
            continue
        leaf = name.rsplit(".", 1)[-1]
        owner = name.rsplit(".", 2)[-2] if "." in name else ""

        if leaf == "from_pretrained" and _HUB_ID.match(arg):
            kind = ResourceKind.HF_TOKENIZER if _is_tokenizer(owner) else ResourceKind.HF_MODEL
            found.add(resource(kind, arg))
        elif leaf == "pipeline" and (model := _keyword_string(node, "model")):
            if _HUB_ID.match(model):
                found.add(resource(ResourceKind.HF_MODEL, model))
        elif leaf == "load_dataset" and arg not in _DATASET_BUILDERS and _HUB_ID.match(arg):
            found.add(resource(ResourceKind.HF_DATASET, arg))
        elif name.endswith("dataset_download") and _KAGGLE_DATASET_ID.match(arg):
            found.add(resource(ResourceKind.KAGGLE_DATASET, arg))
        elif name.endswith("model_download") and arg.count("/") >= 1:
            # A Kaggle model handle is owner/model/framework/variation; the
            # verifiable part is owner/model.
            found.add(resource(ResourceKind.KAGGLE_MODEL, "/".join(arg.split("/")[:2])))
    return found


def _is_tokenizer(owner: str) -> bool:
    return "tokenizer" in owner.lower()


def _call_name(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _first_string(node: ast.Call) -> str | None:
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def _keyword_string(node: ast.Call, name: str) -> str | None:
    for keyword in node.keywords:
        if (
            keyword.arg == name
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return keyword.value.value
    return None
