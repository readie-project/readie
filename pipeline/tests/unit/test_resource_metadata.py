"""Canonical metadata rejects legacy shapes and preserves measured precision."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from readie_pipeline.metadata.models import Metadata, MetadataError, ResourceFacts
from readie_pipeline.resources.identifiers import ResourceType


def test_metadata_round_trips_without_rounding_measured_values(tmp_path):
    facts = ResourceFacts(
        base_import="pandas",
        memory_size_mb=1.234567891234,
        disk_size_mb=9.876543219876,
        import_time=0.0000123456789,
        dependencies=frozenset({"package:numpy"}),
    )
    metadata = Metadata(
        {"package:pandas": facts},
        resolved={"package:pandas": "package:pandas"},
        distributions={"pandas": "2"},
        base_image="base@sha256:a",
    )
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(metadata.to_json()))
    assert Metadata.load(path) == metadata
    assert set(metadata.to_json()) == {"resources", "resolved", "distributions", "base_image"}
    assert "loaded_modules" not in metadata.to_json()["resources"]["package:pandas"]


@pytest.mark.parametrize(
    "payload", [{"packages": {}}, {"pandas": {}}, {"resources": {"pandas": {}}}]
)
def test_legacy_envelopes_and_unqualified_ids_are_rejected(tmp_path, payload):
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(MetadataError):
        Metadata.load(path)


def test_source_type_must_agree_with_its_identifier(tmp_path):
    payload = Metadata({"kaggle_dataset:owner/data": ResourceFacts(base_import="data")}).to_json()
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(MetadataError, match="does not match"):
        Metadata.load(path)


def test_dependency_closures_keep_distinct_sources_with_the_same_name():
    metadata = Metadata(
        {
            "package:datasets": ResourceFacts(base_import="datasets"),
            "kaggle_dataset:owner/data": ResourceFacts(
                base_import="owner/data",
                resource_type=ResourceType.KAGGLE_DATASET,
                dependencies=frozenset({"package:datasets"}),
            ),
        }
    )
    assert metadata.closure(["kaggle_dataset:owner/data"]) == set(metadata.resources)


def test_one_time_migration_preserves_values_unknown_fields_and_dependencies(tmp_path):
    original: dict[str, Any] = {
        "base_image": "base",
        "distributions": {"a": "1"},
        "resolved": {"a.x": "a"},
        "packages": {
            "a": {
                "base_import": "a",
                "resource_type": "package",
                "memory_size_mb": 1.23456789,
                "disk_size_mb": 2.34567891,
                "import_time": 0.000012345,
                "loaded_modules": ["a", "b"],
                "dependencies": ["package:c"],
                "extra": {"keep": True},
            }
        },
    }
    source, target = tmp_path / "old.json", tmp_path / "new.json"
    source.write_text(json.dumps(original))
    script = Path(__file__).parents[2] / "tools" / "migrate_resources.py"
    subprocess.run([sys.executable, str(script), str(source), str(target)], check=True)
    converted = json.loads(target.read_text())
    facts = converted["resources"]["package:a"]
    for field in ["memory_size_mb", "disk_size_mb", "import_time", "extra"]:
        assert facts[field] == original["packages"]["a"][field]
    assert facts["dependencies"] == ["package:b", "package:c"]
    assert converted["resolved"] == {"package:a.x": "package:a"}
    assert converted["base_image"] == "base"
    assert converted["distributions"] == {"a": "1"}


def test_failed_serialization_preserves_the_previous_finished_file(tmp_path, monkeypatch):
    path = tmp_path / "metadata.json"
    path.write_text("previous metadata")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("readie_pipeline.metadata.models.json.dump", fail)
    with pytest.raises(OSError, match="disk full"):
        Metadata().save(path)
    assert path.read_text() == "previous metadata"
    assert not path.with_name(path.name + ".writing").exists()
