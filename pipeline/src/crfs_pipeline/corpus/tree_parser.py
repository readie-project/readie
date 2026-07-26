"""Static analysis of a code snippet.

Extracts the imports, datasets, models and tokenizers a snippet refers to, which
is what the corpus records and what the planner reasons over.

The SDK has a near-identical visitor in ``pkg/src/crfs/_ast.py``. They are
deliberately separate: this one handles notebook-derived source with magics and
``# DATASET USED:`` markers, and the SDK's ships to users who should not inherit
the offline tooling's assumptions.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

#: Keyword arguments that name a pretrained model.
_MODEL_NAME_KEYWORDS = frozenset({"pretrained_model_name_or_path", "model_name"})

#: Corpus snippets record their dataset this way, since there is no import to
#: detect: the generator is instructed to emit exactly one such comment.
_DATASET_MARKER = "# DATASET USED:"


@dataclass(slots=True)
class SnippetFacts:
    """What the visitor found."""

    imports: set[str] = field(default_factory=set)
    datasets: set[str] = field(default_factory=set)
    models: set[str] = field(default_factory=set)
    tokenizers: set[str] = field(default_factory=set)

    def to_json(self) -> dict[str, list[str]]:
        """Sorted lists, so a regenerated corpus diffs cleanly."""
        return {
            "imports": sorted(self.imports),
            "datasets": sorted(self.datasets),
            "models": sorted(self.models),
            "tokenizers": sorted(self.tokenizers),
        }


class _Visitor(ast.NodeVisitor):
    """Collects imports and ``from_pretrained`` names."""

    def __init__(self) -> None:
        self.facts = SnippetFacts()

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


def _string_argument(node: ast.Call) -> str | None:
    """The model name, from the first positional or a known keyword."""
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


def clean(code: str) -> tuple[str, set[str]]:
    """Strip notebook syntax, returning parsable source and any datasets found.

    Snippets come from a model told to imitate notebook code, so they carry
    IPython magics (``%matplotlib``), shell escapes (``!pip install``) and help
    queries (``?obj``). None of that is Python and ``ast.parse`` rejects the
    whole file over one line of it.
    """
    datasets: set[str] = set()
    kept: list[str] = []

    for line in code.splitlines():
        stripped = line.lstrip()

        if stripped.startswith(("%", "!", "?")):
            continue
        if stripped.startswith("#"):
            if stripped.startswith(_DATASET_MARKER):
                name = stripped[len(_DATASET_MARKER) :].strip()
                if name:
                    datasets.add(name)
            continue

        kept.append(line)

    return "\n".join(kept), datasets


def analyse(code: str) -> SnippetFacts:
    """Extract every fact from one snippet.

    Raises ``SyntaxError`` if the cleaned source still will not parse. The
    caller decides what to do about it; the corpus generator skips the snippet
    and says which, rather than failing the batch.
    """
    source, datasets = clean(code)

    visitor = _Visitor()
    visitor.visit(ast.parse(source))
    visitor.facts.datasets |= datasets
    return visitor.facts
