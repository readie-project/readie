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
        alpha=0.01,
        metadata=_metadata(),
        entries=[("checkpoint_1", plan)],
    )

    assert doc["version"] == CATALOGUE_VERSION
    assert doc["flavor"] == "cpu"
    assert doc["alpha"] == 0.01

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
    # No dependencies measured here, so canonical (plan.imports) and the
    # closure (items) coincide -- see the trimmed-plan test below for a case
    # where they differ.
    assert ckpt["canonical"] == ["model:gpt2", "numpy", "pandas"]
    # Raw total size; the router applies its own alpha. 30 + 20 + 500.
    assert ckpt["size_mb"] == pytest.approx(550.0)


def test_catalogue_carries_a_packages_resolved_dependencies() -> None:
    metadata = Metadata(
        {
            "pandas": PackageFacts(
                base_import="pandas",
                distribution="pandas",
                disk_size_mb=30.0,
                import_time=0.25,
                dependencies={"numpy": ">=1.20"},
            ),
            "numpy": PackageFacts(
                base_import="numpy", distribution="numpy", disk_size_mb=20.0, import_time=0.15
            ),
        }
    )
    doc = build_catalogue(
        flavor="cpu",
        alpha=0.01,
        metadata=metadata,
        entries=[("checkpoint_1", CheckpointPlan(imports=("pandas", "numpy")))],
    )

    assert doc["items"]["pandas"]["dependencies"] == ["numpy"]
    # A package with nothing measured to depend on carries no key at all,
    # rather than an empty list every reader has to handle.
    assert "dependencies" not in doc["items"]["numpy"]


def test_catalogue_carries_the_closure_and_the_canonical_list_separately() -> None:
    # plan.imports only ever carries what a request actually asked for -- the
    # planner trims dependencies back out before reporting, since the
    # executor doesn't need them spelled out (Python's own import machinery
    # pulls numpy in as a side effect of `import pandas`). The catalogue needs
    # both: `items`, the full resident set (dependencies included), to price a
    # request's residual correctly, and `canonical`, the untouched
    # request-facing list, for a reader that wants to know what was actually
    # asked for rather than what pricing needed.
    metadata = Metadata(
        {
            "pandas": PackageFacts(
                base_import="pandas",
                distribution="pandas",
                disk_size_mb=30.0,
                import_time=0.25,
                dependencies={"numpy": ">=1.20"},
            ),
            "numpy": PackageFacts(
                base_import="numpy", distribution="numpy", disk_size_mb=20.0, import_time=0.15
            ),
        }
    )
    doc = build_catalogue(
        flavor="cpu",
        alpha=0.01,
        metadata=metadata,
        entries=[("checkpoint_1", CheckpointPlan(imports=("pandas",)))],
    )

    ckpt = doc["checkpoints"][0]
    assert ckpt["items"] == ["numpy", "pandas"]
    assert ckpt["canonical"] == ["pandas"]
    # size_mb prices the full closure, not just the canonical list.
    assert ckpt["size_mb"] == pytest.approx(50.0)


def test_catalogue_round_trips_through_a_file(tmp_path) -> None:
    doc = build_catalogue(
        flavor="gpu",
        alpha=0.01,
        metadata=_metadata(),
        entries=[("checkpoint_1", CheckpointPlan(imports=("pandas",)))],
    )
    path = tmp_path / "catalogue.json"
    write_catalogue(path, doc)

    assert json.loads(path.read_text()) == doc
