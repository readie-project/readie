"""Pulling Python out of Markdown.

HuggingFace cards and many Kaggle write-ups embed runnable code in fenced blocks.
This extracts the Python ones so the agent has real source to adapt.
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"```(?:python|py)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def python_blocks(markdown: str) -> list[str]:
    """Return the contents of every ```python fenced block, non-empty and trimmed."""
    return [block.strip() for block in _FENCE.findall(markdown) if block.strip()]
