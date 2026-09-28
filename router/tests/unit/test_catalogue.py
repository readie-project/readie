"""Request-time checkpoint selection from a catalogue."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from readie_router.scheduling.catalogue import CATALOGUE_VERSION, Catalogue, load_catalogues


def _document(flavor: str = "cpu", **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "version": CATALOGUE_VERSION,
        "flavor": flavor,
        "alpha": 0.005,
        "items": {
            "pandas": {"size_mb": 30.0, "load_time": 0.25, "resource_type": "package"},
            "numpy": {"size_mb": 20.0, "load_time": 0.15, "resource_type": "package"},
            "torch": {"size_mb": 800.0, "load_time": 3.0, "resource_type": "package"},
        },
        "checkpoints": [
            {"id": "c-data", "items": ["pandas", "numpy"], "size_mb": 50.0},
            {"id": "c-torch", "items": ["torch"], "size_mb": 800.0},
        ],
    }
    document.update(overrides)
    return document


def test_select_picks_the_lowest_cost_checkpoint() -> None:
    catalogue = Catalogue.from_document(_document())

    # c-data covers pandas+numpy: cost 0.005*50 + 0 = 0.25.
    # c-torch: 0.005*800 + (0.25+0.15) = 4.0+0.4 = 4.4.  cold: 0.4.
    assert catalogue.select(["pandas", "numpy"]) == "c-data"


def test_unknown_required_items_do_not_change_the_winner() -> None:
    catalogue = Catalogue.from_document(_document())

    # "flask" is measured nowhere: it is in no checkpoint and adds the same
    # constant to every option, so the winner is unchanged.
    assert catalogue.select(["pandas", "numpy", "flask"]) == "c-data"


def test_an_empty_catalogue_always_cold_starts() -> None:
    assert Catalogue().select(["pandas"]) == ""


def test_catalogues_load_from_a_directory_keyed_by_flavor(tmp_path: Path) -> None:
    (tmp_path / "cpu.json").write_text(json.dumps(_document("cpu")))
    (tmp_path / "gpu.json").write_text(json.dumps(_document("gpu")))

    catalogues = load_catalogues(tmp_path)

    assert set(catalogues) == {"cpu", "gpu"}
    assert catalogues["cpu"].select(["pandas", "numpy"]) == "c-data"


def test_a_missing_directory_yields_no_catalogues() -> None:
    assert load_catalogues(None) == {}


# ---------------------------------------------------------------------------
# Dependency closures
# ---------------------------------------------------------------------------
def _document_with_dependencies(**overrides: Any) -> dict[str, Any]:
    return _document(
        items={
            "pandas": {
                "size_mb": 30.0,
                "load_time": 0.25,
                "resource_type": "package",
                "dependencies": ["numpy"],
            },
            "numpy": {"size_mb": 20.0, "load_time": 0.15, "resource_type": "package"},
        },
        checkpoints=[{"id": "c-numpy", "items": ["numpy"], "size_mb": 20.0}],
        **overrides,
    )


def test_closure_expands_a_required_item_through_its_dependencies() -> None:
    catalogue = Catalogue.from_document(_document_with_dependencies())
    assert catalogue.closure(["pandas"]) == {"pandas", "numpy"}


def test_closure_visits_a_shared_dependency_once() -> None:
    document = _document_with_dependencies()
    document["items"]["scipy"] = {
        "size_mb": 50.0,
        "load_time": 0.5,
        "resource_type": "package",
        "dependencies": ["numpy"],
    }
    catalogue = Catalogue.from_document(document)
    assert catalogue.closure(["pandas", "scipy"]) == {"pandas", "scipy", "numpy"}


def test_select_prices_a_required_items_dependency_even_when_not_named_directly() -> None:
    # A request for "pandas" alone still needs numpy loaded; a checkpoint that
    # already carries numpy should be credited for that even though the
    # request never names numpy itself.
    #
    # A higher alpha than the module default: at 0.005 a dedicated 20 MB
    # checkpoint for just numpy would be worth it (0.1+0.25=0.35 < cold 0.4),
    # which would demonstrate the opposite of what this test is for.
    catalogue = Catalogue.from_document(_document_with_dependencies(alpha=0.01))

    # cold: pandas (0.25) + numpy (0.15) = 0.4.
    # c-numpy: alpha*20 (0.2) + residual pandas only (0.25) = 0.45 -- not worth it.
    assert catalogue.select(["pandas"]) == ""


def test_a_bad_or_wrong_version_file_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{ not json")
    (tmp_path / "old.json").write_text(json.dumps(_document("cpu", version=999)))
    skipped: list[str] = []

    catalogues = load_catalogues(tmp_path, warn=lambda path, _reason: skipped.append(path))

    assert catalogues == {}
    assert len(skipped) == 2
