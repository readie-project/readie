"""The checkpoint catalogue the router selects from."""

from __future__ import annotations

import json

import pytest

from readie_pipeline.catalogue import (
    CATALOGUE_VERSION,
    build_catalogue,
    item_key,
    write_catalogue,
)
from readie_pipeline.metadata.models import Metadata, PackageFacts, ResourceType
from readie_pipeline.planning.ports import CheckpointPlan


def _metadata() -> Metadata:
    return Metadata(
        {
            "pandas": PackageFacts(base_import="pandas", disk_size_mb=30.0, import_time=0.25),
            "numpy": PackageFacts(base_import="numpy", disk_size_mb=20.0, import_time=0.15),
            "gpt2": PackageFacts(
                base_import="gpt2",
                disk_size_mb=500.0,
                import_time=2.0,
                resource_type=ResourceType.MODEL,
            ),
        }
    )


def test_items_are_keyed_by_kind() -> None:
    assert item_key("pandas", ResourceType.PACKAGE) == "pandas"
    assert item_key("gpt2", ResourceType.MODEL) == "model:gpt2"
    assert item_key("squad", ResourceType.DATASET) == "dataset:squad"


def test_catalogue_carries_item_costs_and_precomputed_checkpoint_size() -> None:
    plan = CheckpointPlan(imports=("pandas", "numpy"), models=("gpt2",))
    doc = build_catalogue(
        flavor="cpu",
        alpha=0.002,
        metadata=_metadata(),
        entries=[("checkpoint_1", plan)],
    )

    assert doc["version"] == CATALOGUE_VERSION
    assert doc["flavor"] == "cpu"
    assert doc["alpha"] == 0.002

    # Every measured item, keyed the way a request names it.
    assert doc["items"]["pandas"] == {
        "size_mb": 30.0,
        "load_time": 0.25,
        "resource_type": "package",
    }
    assert doc["items"]["model:gpt2"]["resource_type"] == "model"

    ckpt = doc["checkpoints"][0]
    assert ckpt["id"] == "checkpoint_1"
    assert ckpt["items"] == ["model:gpt2", "numpy", "pandas"], "sorted, kind-prefixed"
    # Raw total size; the router applies its own alpha. 30 + 20 + 500.
    assert ckpt["size_mb"] == pytest.approx(550.0)


def test_catalogue_round_trips_through_a_file(tmp_path) -> None:
    doc = build_catalogue(
        flavor="gpu",
        alpha=0.002,
        metadata=_metadata(),
        entries=[("checkpoint_1", CheckpointPlan(imports=("pandas",)))],
    )
    path = tmp_path / "catalogue.json"
    write_catalogue(path, doc)

    assert json.loads(path.read_text()) == doc
