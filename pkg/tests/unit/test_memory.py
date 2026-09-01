"""Parsing human-written memory sizes."""

from __future__ import annotations

import pytest

from readie._memory import parse_memory
from readie.errors import ConfigurationError


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 0),
        (0, 0),
        (1024, 1024),
        ("", 0),
        ("512", 512),
        ("512Mi", 512 * 1024 * 1024),
        ("2Gi", 2 * 1024 * 1024 * 1024),
        ("4GiB", 4 * 1024 * 1024 * 1024),
        ("1Ti", 1024**4),
        ("500MB", 500 * 1_000_000),
        ("8G", 8 * 1_000_000_000),
        ("  256Mi  ", 256 * 1024 * 1024),
    ],
)
def test_sizes_parse_to_bytes(value: str | int | None, expected: int) -> None:
    assert parse_memory(value) == expected


@pytest.mark.parametrize("value", ["banana", "12x", "Mi", "-5", -1, True])
def test_bad_sizes_are_rejected(value: object) -> None:
    with pytest.raises(ConfigurationError):
        parse_memory(value)  # type: ignore[arg-type]
