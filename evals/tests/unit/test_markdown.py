from __future__ import annotations

from readie_evals.fetch._markdown import python_blocks


def test_extracts_python_fenced_blocks():
    md = "text\n```python\nimport numpy\n```\nmore\n```py\nx = 1\n```\n"
    assert python_blocks(md) == ["import numpy", "x = 1"]


def test_ignores_non_python_fences():
    assert python_blocks("```bash\nls\n```\n") == []


def test_no_blocks_returns_empty():
    assert python_blocks("just prose") == []
