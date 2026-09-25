from __future__ import annotations

import pytest

from readie_evals.agent._json import ResponseError, extract_json


def test_parses_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_strips_a_markdown_fence():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_recovers_json_embedded_in_prose():
    assert extract_json('Sure! Here it is: {"a": [1, 2]} done') == {"a": [1, 2]}


def test_raises_when_there_is_no_json():
    with pytest.raises(ResponseError):
        extract_json("no json at all")
