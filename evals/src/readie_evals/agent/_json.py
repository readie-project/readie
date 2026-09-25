"""Pulling a JSON value out of a model response.

The model is asked to return only JSON, but responses sometimes arrive wrapped in
a Markdown code fence or with a line of preamble. This finds the JSON payload
rather than failing the whole batch over a stray backtick.
"""

from __future__ import annotations

import json
from typing import Any

#: A fenced block has at least an opening and a closing fence line to strip.
_FENCE_MIN_LINES = 2


class ResponseError(Exception):
    """The model did not return the JSON that was asked for."""


def extract_json(raw: str) -> Any:
    """Parse the first JSON object or array in a model response.

    Strips a leading/trailing Markdown code fence, then falls back to the span
    between the first opening and last closing bracket.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Drop the opening fence line (``` or ```json) and the closing fence.
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]) if len(lines) >= _FENCE_MIN_LINES else text.strip("`")
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    end = max(text.rfind("}"), text.rfind("]"))
    if start == -1 or end <= start:
        msg = "no JSON object or array found in the model response"
        raise ResponseError(msg)
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        msg = f"model returned invalid JSON: {exc}"
        raise ResponseError(msg) from exc
