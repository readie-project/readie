"""Normalizing the ``packages=`` list on ``@remote``."""

from __future__ import annotations

import pytest

from readie.errors import InvalidPackageError
from readie.packages import normalize_packages


def test_bare_names_pass_through() -> None:
    assert normalize_packages(["numpy", "requests"]) == ("numpy", "requests")


def test_version_specifiers_pass_through_unchanged() -> None:
    assert normalize_packages(["numpy==1.26.0"]) == ("numpy==1.26.0",)


def test_case_and_separator_variants_dedupe_to_one_entry() -> None:
    # PEP 503: lowercase, runs of -._ collapsed to a single -.
    result = normalize_packages(["scikit_learn", "Scikit-Learn", "scikit.learn"])
    assert len(result) == 1


def test_first_occurrence_wins_on_dedupe() -> None:
    assert normalize_packages(["Requests", "requests==2.31.0"]) == ("Requests",)


def test_result_is_sorted_by_normalized_name() -> None:
    assert normalize_packages(["requests", "numpy"]) == ("numpy", "requests")


def test_blank_and_whitespace_only_entries_are_dropped() -> None:
    assert normalize_packages(["numpy", "", "   "]) == ("numpy",)


def test_empty_input_yields_empty_output() -> None:
    assert normalize_packages(()) == ()


def test_leading_and_trailing_whitespace_is_stripped() -> None:
    assert normalize_packages(["  numpy  "]) == ("numpy",)


def test_an_invalid_requirement_string_raises() -> None:
    with pytest.raises(InvalidPackageError):
        normalize_packages(["not a valid requirement!!"])
