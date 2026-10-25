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
from readie_pipeline.metadata.inventory import Inventory
from readie_pipeline.metadata.models import Metadata, PackageFacts, ResourceType
from readie_pipeline.planning.ports import CheckpointPlan


def _inventory(*distributions: str) -> Inventory:
    """A rootfs that provides these distributions, each also as its own import name."""
    return Inventory(
        provided={name: [name] for name in distributions},
        distributions=frozenset(distributions),
    )


_ROOTFS = _inventory("pandas", "numpy")


def _metadata() -> Metadata:
    return Metadata(
        {
            "pandas": PackageFacts(
                base_import="pandas", disk_size_mb=30.0, memory_size_mb=30.0, import_time=0.25
            ),
            "numpy": PackageFacts(
                base_import="numpy", disk_size_mb=20.0, memory_size_mb=20.0, import_time=0.15
            ),
            "gpt2": PackageFacts(
                base_import="gpt2",
                disk_size_mb=500.0,
                memory_size_mb=500.0,
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
        alpha=0.005,
        metadata=_metadata(),
        entries=[("checkpoint_1", plan)],
        inventory=_ROOTFS,
    )

    assert doc["version"] == CATALOGUE_VERSION
    assert doc["flavor"] == "cpu"
    assert doc["alpha"] == 0.005

    # Every measured item, keyed the way a request names it.
    assert doc["items"]["pandas"] == {
        "size_mb": 30.0,
        "load_time": 0.25,
        "resource_type": "package",
        "distribution": "",
    }
    assert doc["items"]["model:gpt2"]["resource_type"] == "model"
    # Only a package has a distribution; the rest default to blank.
    assert doc["items"]["model:gpt2"]["distribution"] == ""
    # The rootfs inventory rides along for the router's package filtering.
    assert doc["distributions"] == {"numpy": "", "pandas": ""}

    ckpt = doc["checkpoints"][0]
    assert ckpt["id"] == "checkpoint_1"
    assert ckpt["items"] == ["model:gpt2", "numpy", "pandas"], "sorted, kind-prefixed"
    # No dependencies measured here, so canonical (plan.imports) and the
    # closure (items) coincide -- see the trimmed-plan test below for a case
    # where they differ.
    assert ckpt["canonical"] == ["model:gpt2", "numpy", "pandas"]
    # Raw total size; the router applies its own alpha. 30 + 20 + 500.
    assert ckpt["size_mb"] == pytest.approx(550.0)


def test_catalogue_uses_source_qualified_checkpoint_keys() -> None:
    metadata = Metadata(
        {
            "kaggle_dataset:owner/data": PackageFacts(
                base_import="owner/data",
                memory_size_mb=4.0,
                resource_type=ResourceType.KAGGLE_DATASET,
            ),
            "hf_model:gpt2": PackageFacts(
                base_import="gpt2",
                memory_size_mb=8.0,
                resource_type=ResourceType.HF_MODEL,
            ),
        }
    )
    plan = CheckpointPlan(datasets=("owner/data",), models=("gpt2",))

    document = build_catalogue(
        flavor="cpu",
        alpha=0.005,
        metadata=metadata,
        entries=[("checkpoint", plan)],
        inventory=None,
    )

    checkpoint = document["checkpoints"][0]
    assert checkpoint["items"] == ["hf_model:gpt2", "kaggle_dataset:owner/data"]
    assert checkpoint["canonical"] == ["hf_model:gpt2", "kaggle_dataset:owner/data"]
    assert checkpoint["size_mb"] == pytest.approx(12.0)


def test_catalogue_carries_a_packages_resolved_dependencies() -> None:
    metadata = Metadata(
        {
            "pandas": PackageFacts(
                base_import="pandas",
                distribution="pandas",
                disk_size_mb=30.0,
                import_time=0.25,
                loaded_modules=frozenset({"pandas", "numpy"}),
            ),
            "numpy": PackageFacts(
                base_import="numpy",
                distribution="numpy",
                disk_size_mb=20.0,
                import_time=0.15,
                loaded_modules=frozenset({"numpy"}),
            ),
        }
    )
    doc = build_catalogue(
        flavor="cpu",
        alpha=0.005,
        metadata=metadata,
        entries=[("checkpoint_1", CheckpointPlan(imports=("pandas", "numpy")))],
        inventory=_ROOTFS,
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
                memory_size_mb=30.0,
                import_time=0.25,
                loaded_modules=frozenset({"pandas", "numpy"}),
            ),
            "numpy": PackageFacts(
                base_import="numpy",
                distribution="numpy",
                disk_size_mb=20.0,
                memory_size_mb=20.0,
                import_time=0.15,
                loaded_modules=frozenset({"numpy"}),
            ),
        }
    )
    doc = build_catalogue(
        flavor="cpu",
        alpha=0.005,
        metadata=metadata,
        entries=[("checkpoint_1", CheckpointPlan(imports=("pandas",)))],
        inventory=_ROOTFS,
    )

    ckpt = doc["checkpoints"][0]
    assert ckpt["items"] == ["numpy", "pandas"]
    assert ckpt["canonical"] == ["pandas"]
    # size_mb prices the full closure, not just the canonical list.
    assert ckpt["size_mb"] == pytest.approx(50.0)


def test_catalogue_round_trips_through_a_file(tmp_path) -> None:
    doc = build_catalogue(
        flavor="gpu",
        alpha=0.005,
        metadata=_metadata(),
        entries=[("checkpoint_1", CheckpointPlan(imports=("pandas",)))],
        inventory=_ROOTFS,
    )
    path = tmp_path / "catalogue.json"
    write_catalogue(path, doc)

    assert json.loads(path.read_text()) == doc


def test_catalogue_ports_only_what_the_rootfs_provides() -> None:
    # The metadata can be measured in a different image than the one the
    # sandbox runs, so a package whose distribution the rootfs lacks must not
    # reach the catalogue, nor linger in a dependency list or a checkpoint.
    metadata = Metadata(
        {
            "pandas": PackageFacts(
                base_import="pandas",
                distribution="pandas",
                memory_size_mb=30.0,
                import_time=0.25,
                loaded_modules=frozenset({"pandas", "scipy", "os"}),
            ),
            "scipy": PackageFacts(
                base_import="scipy", distribution="scipy", memory_size_mb=40.0, import_time=0.3
            ),
            "gpt2": PackageFacts(
                base_import="gpt2",
                memory_size_mb=500.0,
                import_time=2.0,
                resource_type=ResourceType.MODEL,
            ),
        }
    )
    plan = CheckpointPlan(imports=("pandas", "scipy"), models=("gpt2",))
    doc = build_catalogue(
        flavor="cpu",
        alpha=0.005,
        metadata=metadata,
        entries=[("checkpoint_1", plan)],
        inventory=_inventory("pandas"),
    )

    assert sorted(doc["items"]) == ["model:gpt2", "pandas"]
    # scipy is gone, but an unmeasured name (a stdlib module) is left alone.
    assert doc["items"]["pandas"]["dependencies"] == ["os"]
    ckpt = doc["checkpoints"][0]
    # The closure keeps the unmeasured `os`, as it always has; only scipy goes.
    assert ckpt["items"] == ["model:gpt2", "os", "pandas"]
    assert ckpt["canonical"] == ["model:gpt2", "pandas"]
    assert ckpt["size_mb"] == pytest.approx(530.0)


def test_a_package_is_matched_the_way_analyze_resolves_its_distribution() -> None:
    # `analyze` takes the first provider of the top-level import name. The
    # catalogue resolves the rootfs the same way (distribution_for), so a
    # distribution spelled differently in the metadata (blank, or `pillow`
    # against `Pillow`) still matches, and a name the rootfs does not provide
    # does not.
    metadata = Metadata(
        {
            "yaml": PackageFacts(base_import="yaml", memory_size_mb=5.0, import_time=0.1),
            "PIL": PackageFacts(
                base_import="PIL", distribution="pillow", memory_size_mb=5.0, import_time=0.1
            ),
            "ghost": PackageFacts(base_import="ghost", memory_size_mb=5.0, import_time=0.1),
        }
    )
    inventory = Inventory(
        provided={"yaml": ["PyYAML"], "PIL": ["Pillow"]},
        distributions=frozenset({"pyyaml", "pillow"}),
    )
    doc = build_catalogue(
        flavor="cpu", alpha=0.005, metadata=metadata, entries=[], inventory=inventory
    )

    assert sorted(doc["items"]) == ["PIL", "yaml"]
    assert doc["items"]["PIL"]["distribution"] == "pillow", "the metadata's own, unchanged"
    assert doc["distributions"] == {"pillow": "", "pyyaml": ""}


def test_each_distribution_maps_to_the_version_the_rootfs_has() -> None:
    inventory = Inventory(
        provided={"yaml": ["PyYAML"]},
        distributions=frozenset({"pyyaml", "pillow"}),
        versions={"pyyaml": "6.0.3", "pillow": "11.3.0"},
    )

    doc = build_catalogue(
        flavor="cpu", alpha=0.005, metadata=Metadata({}), entries=[], inventory=inventory
    )

    # The rootfs's own versions, not the metadata's: what the image really has.
    assert doc["distributions"] == {"pillow": "11.3.0", "pyyaml": "6.0.3"}
    assert "distribution_versions" not in doc, "one key: the names are the map's keys"


def test_a_distribution_without_a_version_is_still_listed_so_the_router_filters_it() -> None:
    inventory = Inventory(
        provided={}, distributions=frozenset({"numpy", "wheel"}), versions={"numpy": "2.0.2"}
    )

    doc = build_catalogue(
        flavor="cpu", alpha=0.005, metadata=Metadata({}), entries=[], inventory=inventory
    )

    assert doc["distributions"] == {"numpy": "2.0.2", "wheel": ""}


def test_a_rootfs_that_could_not_be_listed_leaves_the_catalogue_unfiltered() -> None:
    # No inventory means unknown, not empty: dropping everything would write a
    # catalogue with no items, so nothing is dropped and no distributions listed.
    metadata = Metadata({"ghost": PackageFacts(base_import="ghost", memory_size_mb=5.0)})
    doc = build_catalogue(flavor="cpu", alpha=0.005, metadata=metadata, entries=[], inventory=None)

    assert sorted(doc["items"]) == ["ghost"]
    assert doc["distributions"] == {}
