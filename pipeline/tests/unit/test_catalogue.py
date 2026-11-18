"""Catalogue pricing uses the same resource IDs as metadata and plans."""

from __future__ import annotations

import json
from typing import Any

from readie_pipeline.catalogue import CATALOGUE_VERSION, build_catalogue, write_catalogue
from readie_pipeline.metadata.inventory import Inventory
from readie_pipeline.metadata.models import Metadata, ResourceFacts
from readie_pipeline.planning.ports import CheckpointPlan
from readie_pipeline.resources.identifiers import ResourceType


def metadata() -> Metadata:
    return Metadata(
        {
            "package:pandas": ResourceFacts(
                base_import="pandas",
                distribution="pandas",
                memory_size_mb=30,
                import_time=0.25,
                dependencies=frozenset({"package:numpy"}),
            ),
            "package:numpy": ResourceFacts(
                base_import="numpy", distribution="numpy", memory_size_mb=20, import_time=0.15
            ),
            "kaggle_dataset:owner/data": ResourceFacts(
                base_import="owner/data",
                resource_type=ResourceType.KAGGLE_DATASET,
                memory_size_mb=50,
                import_time=2,
                dependencies=frozenset({"package:pandas", "package:numpy"}),
            ),
        }
    )


def catalogue(inventory: Inventory | None = None) -> dict[str, Any]:
    return build_catalogue(
        flavor="cpu",
        alpha=0.005,
        metadata=metadata(),
        entries=[("checkpoint_1", CheckpointPlan(resources=("package:pandas",)))],
        inventory=inventory,
    )


def test_resource_ids_and_dependency_costs_are_preserved() -> None:
    doc = catalogue()
    assert doc["version"] == CATALOGUE_VERSION
    assert doc["items"]["package:pandas"]["dependencies"] == ["package:numpy"]
    checkpoint = doc["checkpoints"][0]
    assert checkpoint["canonical"] == ["package:pandas"]
    assert checkpoint["items"] == ["package:numpy", "package:pandas"]
    assert checkpoint["size_mb"] == 50
    assert doc["items"]["package:pandas"]["load_time"] == 0.25


def test_packages_missing_from_rootfs_are_removed_from_all_references() -> None:
    inventory = Inventory(provided={"pandas": ["pandas"]}, distributions=frozenset({"pandas"}))
    doc = catalogue(inventory)
    assert "package:numpy" not in doc["items"]
    assert "dependencies" not in doc["items"]["package:pandas"]
    assert doc["checkpoints"][0]["items"] == ["package:pandas"]
    assert doc["checkpoints"][0]["size_mb"] == 30
    assert "kaggle_dataset:owner/data" in doc["items"]


def test_distribution_matching_uses_the_import_name_not_the_distribution_spelling() -> None:
    inventory = Inventory(
        provided={"yaml": ["PyYAML"], "PIL": ["Pillow"]},
        distributions=frozenset({"pyyaml", "pillow"}),
        versions={"pyyaml": "6.0.3"},
    )
    facts = Metadata(
        {
            "package:yaml": ResourceFacts(base_import="yaml", distribution="PyYAML"),
            "package:PIL": ResourceFacts(base_import="PIL", distribution="pillow"),
            "package:ghost": ResourceFacts(base_import="ghost"),
        }
    )
    doc = build_catalogue(
        flavor="cpu", alpha=0.005, metadata=facts, entries=[], inventory=inventory
    )
    assert set(doc["items"]) == {"package:yaml", "package:PIL"}
    assert doc["distributions"] == {"pillow": "", "pyyaml": "6.0.3"}


def test_unknown_rootfs_keeps_all_resource_facts() -> None:
    doc = catalogue()
    assert set(doc["items"]) == set(metadata().resources)
    assert doc["distributions"] == {}


def test_size_uses_memory_instead_of_disk() -> None:
    facts = Metadata(
        {"package:a": ResourceFacts(base_import="a", disk_size_mb=999, memory_size_mb=2)}
    )
    doc = build_catalogue(
        flavor="cpu",
        alpha=0.005,
        metadata=facts,
        entries=[("a", CheckpointPlan(resources=("package:a",)))],
        inventory=None,
    )
    assert doc["checkpoints"][0]["size_mb"] == 2


def test_writing_a_catalogue_is_atomic_and_round_trips(tmp_path) -> None:
    path = tmp_path / "nested" / "catalogue.json"
    write_catalogue(path, catalogue())
    assert json.loads(path.read_text()) == catalogue()
    assert not path.with_suffix(".json.partial").exists()
