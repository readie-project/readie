"""Pull runnable code out of notebooks and Markdown."""

from __future__ import annotations

import json
import re

from readie_pipeline.corpus.extract import clean_cell

_FENCED_PYTHON = re.compile(r"```(?:python|py)\s*\n(.*?)```", re.DOTALL)


def notebook_cells(text: str) -> tuple[str, ...]:
    """The non-empty code cells of an ``.ipynb`` document, in order.

    Returns an empty tuple when the text is not a notebook.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return ()
    cells = document.get("cells") if isinstance(document, dict) else None
    if not isinstance(cells, list):
        return ()

    code: list[str] = []
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        source = cell.get("source", "")
        body = "".join(source) if isinstance(source, list) else str(source)
        if clean_cell(body).strip():
            code.append(body)
    return tuple(code)


def python_blocks(markdown: str) -> list[str]:
    """Fenced Python blocks in a Markdown document."""
    return [m.group(1).strip("\n") for m in _FENCED_PYTHON.finditer(markdown)]
