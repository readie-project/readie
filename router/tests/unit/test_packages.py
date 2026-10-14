"""The router's authoritative normalization of a request's ``packages``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from readie_router.errors import InvalidRequestError
from readie_router.scheduling.packages import normalize_packages, without_installed

# The client normalizes the same list independently (readie.packages); this
# fixture is what keeps the two in step, and the pkg suite runs it as well.
_VECTORS = json.loads(
    (Path(__file__).parents[3] / "pkg" / "tests" / "data" / "package_specs.json").read_text()
)


@pytest.mark.parametrize("case", _VECTORS["valid"], ids=lambda case: repr(case["input"]))
def test_shared_vectors_normalize_as_the_client_does(case: dict[str, list[str]]) -> None:
    assert normalize_packages(case["input"]) == tuple(case["expected"])


@pytest.mark.parametrize("specs", _VECTORS["invalid"], ids=repr)
def test_shared_invalid_vectors_are_rejected_as_the_client_rejects_them(specs: list[str]) -> None:
    # A spec such as "--index-url=..." must never reach uv as an option.
    with pytest.raises(InvalidRequestError):
        normalize_packages(specs)


def test_a_distribution_the_base_image_has_is_dropped_whatever_version_is_asked() -> None:
    kept, ignored = without_installed(["numpy==2.0", "Requests", "flask"], {"numpy", "requests"})

    assert kept == ("flask",)
    assert ignored == ("numpy==2.0", "Requests")


def test_a_spec_with_extras_is_kept_so_the_extras_dependencies_get_installed() -> None:
    kept, ignored = without_installed(["requests[socks]"], {"requests"})

    assert kept == ("requests[socks]",)
    assert ignored == ()


def test_nothing_is_dropped_when_the_image_inventory_is_unknown() -> None:
    kept, ignored = without_installed(["numpy"], frozenset())

    assert kept == ("numpy",)
    assert ignored == ()
