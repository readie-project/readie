"""Request-time checkpoint selection from a catalogue."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from crfs_router.scheduling.catalogue import CATALOGUE_VERSION, Catalogue, load_catalogues


def _document(flavor: str = "cpu", **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "version": CATALOGUE_VERSION,
        "flavor": flavor,
        "alpha": 0.002,
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

    # c-data covers pandas+numpy: cost 0.002*50 + 0 = 0.1.
    # c-torch: 0.002*800 + (0.25+0.15) = 2.0.  cold: 0.4.
    assert catalogue.select(["pandas", "numpy"]) == "c-data"


def test_select_falls_back_to_a_cold_start_when_no_checkpoint_is_worth_it() -> None:
    # A high alpha makes even a modest checkpoint cost more than the import it
    # would save, so the cheapest option is no checkpoint at all.
    catalogue = Catalogue.from_document(_document())

    # numpy alone saves 0.15 s; c-data costs 0.02*50 = 1.0. Cold (0.15) wins.
    assert catalogue.select(["numpy"]) == ""


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


def test_a_bad_or_wrong_version_file_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{ not json")
    (tmp_path / "old.json").write_text(json.dumps(_document("cpu", version=999)))
    skipped: list[str] = []

    catalogues = load_catalogues(
        tmp_path, warn=lambda path, _reason: skipped.append(path)
    )

    assert catalogues == {}
    assert len(skipped) == 2
