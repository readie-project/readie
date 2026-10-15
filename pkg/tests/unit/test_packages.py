"""Normalizing the ``packages=`` list on ``@remote``."""

from __future__ import annotations

import json
from pathlib import Path

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


_VECTORS = json.loads((Path(__file__).parent.parent / "data" / "package_specs.json").read_text())


@pytest.mark.parametrize("case", _VECTORS["valid"], ids=lambda case: repr(case["input"]))
def test_shared_vectors_normalize_as_the_router_does(case: dict[str, list[str]]) -> None:
    # The router normalizes the same list again; both suites run this file.
    assert normalize_packages(case["input"]) == tuple(case["expected"])


@pytest.mark.parametrize("specs", _VECTORS["invalid"], ids=repr)
def test_shared_invalid_vectors_are_rejected_as_the_router_rejects_them(specs: list[str]) -> None:
    with pytest.raises(InvalidPackageError):
        normalize_packages(specs)
