"""Static analysis of a decorated function's source.

The visitor shape follows ``pipeline``'s ``corpus/tree_parser.py``, which does this for
corpus notebooks. It is reimplemented rather than imported: the SDK ships to
users, and coupling it to the offline analysis tooling -- and to that tooling's
dependencies and its notebook-specific line cleaning -- costs more than the
duplication.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

_MODEL_NAME_KEYWORDS = frozenset({"pretrained_model_name_or_path", "model_name"})


@dataclass(slots=True)
class SourceFacts:
    """Everything the visitor found."""

    imports: set[str] = field(default_factory=set)
    models: set[str] = field(default_factory=set)
    tokenizers: set[str] = field(default_factory=set)
    assignments: dict[str, str] = field(default_factory=dict)


class _Visitor(ast.NodeVisitor):
    """Collects imports, assignments and ``from_pretrained`` model names."""

    def __init__(self) -> None:
        self.facts = SourceFacts()

    def visit_Import(self, node: ast.Import) -> None:
        """Record ``import x, y as z``, keeping the real module name."""
        for alias in node.names:
            self.facts.imports.add(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Record ``from x import y``, ignoring relative imports."""
        # node.module is None for `from . import x`, which names no module.
        if node.module and node.level == 0:
            self.facts.imports.add(node.module)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Record the model or tokenizer named by a ``from_pretrained`` call."""
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "from_pretrained":
            owner = func.value
            if isinstance(owner, ast.Name):
                name = _string_argument(node)
                if name:
                    if "Tokenizer" in owner.id:
                        self.facts.tokenizers.add(name)
                    elif "Model" in owner.id or "Pipeline" in owner.id:
                        self.facts.models.add(name)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Record simple ``name = <literal>`` bindings and their types."""
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.facts.assignments[target.id] = _describe(node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Record an annotated binding, preferring the written annotation."""
        if isinstance(node.target, ast.Name):
            self.facts.assignments[node.target.id] = ast.unparse(node.annotation)
        self.generic_visit(node)


def _string_argument(node: ast.Call) -> str | None:
    """Return the model name from the first positional or a known keyword."""
    if node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    for keyword in node.keywords:
        if keyword.arg in _MODEL_NAME_KEYWORDS:
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    return None


_LITERAL_TYPES: tuple[tuple[type[ast.expr] | tuple[type[ast.expr], ...], str], ...] = (
    (ast.Dict, "dict"),
    ((ast.List, ast.ListComp), "list"),
    ((ast.Set, ast.SetComp), "set"),
    (ast.Tuple, "tuple"),
)


def _describe(node: ast.expr) -> str:
    """Name the type of an expression, when it is knowable without running it."""
    if isinstance(node, ast.Constant):
        return type(node.value).__name__
    if isinstance(node, ast.Call):
        # The callee is the best available guess: `np.zeros(3)` says "np.zeros".
        return ast.unparse(node.func)
    for kinds, name in _LITERAL_TYPES:
        if isinstance(node, kinds):
            return name
    return ""


def analyse(source: str) -> SourceFacts:
    """Parse ``source`` and collect its facts.

    Raises ``SyntaxError`` -- the caller decides whether an unparsable function
    is worth failing a call over. It is not.
    """
    visitor = _Visitor()
    visitor.visit(ast.parse(source))
    return visitor.facts
