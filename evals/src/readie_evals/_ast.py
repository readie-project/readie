"""Static import extraction for a code snippet.

Mirrors ``readie_pipeline.corpus.tree_parser`` and ``readie._ast``: a tiny AST
visitor, deliberately duplicated rather than imported so this component does not
inherit the pipeline's dependencies or the SDK's user-facing constraints. It also
strips the notebook syntax (magics, shell escapes) that fetched Kaggle code often
carries, so ``ast.parse`` does not reject a whole file over one such line.
"""

from __future__ import annotations

import ast


def strip_notebook_syntax(code: str) -> str:
    """Drop IPython magics, shell escapes and help queries from source.

    Fetched notebook code carries ``%matplotlib``, ``!pip install`` and ``?obj``
    lines that are not Python. None of them survive into a remote function, so
    they are removed before parsing rather than causing a ``SyntaxError``.
    """
    kept = [line for line in code.splitlines() if not line.lstrip().startswith(("%", "!", "?"))]
    return "\n".join(kept)


class _ImportVisitor(ast.NodeVisitor):
    """Collects the top-level module of every absolute import."""

    def __init__(self) -> None:
        self.modules: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        """Record ``import x.y as z`` as ``x``."""
        for alias in node.names:
            self.modules.add(alias.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Record ``from x.y import z`` as ``x``, skipping relative imports."""
        if node.module and node.level == 0:
            self.modules.add(node.module.split(".")[0])
        self.generic_visit(node)


def extract_imports(code: str) -> tuple[str, ...]:
    """Return the sorted top-level modules ``code`` imports.

    Raises:
        SyntaxError: if the source will not parse even after notebook syntax is
            stripped. The caller decides whether that is worth discarding the
            snippet over.
    """
    visitor = _ImportVisitor()
    visitor.visit(ast.parse(strip_notebook_syntax(code)))
    return tuple(sorted(visitor.modules))
