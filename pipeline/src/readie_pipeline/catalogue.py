"""Write the checkpoint catalogue from canonical resource IDs and measured costs.

Item IDs, dependencies and checkpoint resources share one namespace. The catalogue
expands each plan through measured dependencies and prices resident memory, not disk.
Only packages available in the target rootfs are retained. Source-qualified IDs
remain intact; the router normalizes SDK import hints at its input boundary.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from readie_pipeline.metadata.inventory import Inventory, distribution_for
from readie_pipeline.metadata.models import Metadata
from readie_pipeline.planning.ports import CheckpointPlan
from readie_pipeline.resources.identifiers import ResourceType

#: The on-disk format version, so a router can refuse a catalogue it cannot read.
CATALOGUE_VERSION = 2


def _checkpoint_items(plan: CheckpointPlan, metadata: Metadata) -> list[str]:
    """Every item a checkpoint actually holds resident, keyed for the catalogue.

    ``plan.resources`` is deliberately just what some request actually asked
    for -- what the executor is told to import, since Python's own import
    machinery pulls in the rest -- not the full dependency closure the
    planner scored it against. This reconstructs that closure: a request-time
    residual has to be priced against everything actually resident in this
    checkpoint, dependencies included, not just the trimmed executor-facing
    list (see ``_canonical_items`` for that one).
    """
    return sorted(metadata.closure(plan.resources))


def _canonical_items(plan: CheckpointPlan) -> list[str]:
    """The packages some request actually asked for, keyed for the catalogue.

    Not expanded through dependencies -- this is the same list the executor
    was told to import (``plan.resources``), kept alongside the full closure
    (``_checkpoint_items``) for a reader that wants to know what was actually
    requested rather than what pricing needed.
    """
    return sorted(plan.resources)


def _in_rootfs(name: str, resource_type: ResourceType, inventory: Inventory) -> bool:
    """Whether the rootfs provides what a metadata entry describes.

    A package is kept when the rootfs has a distribution for it, resolved the way
    ``analyze`` resolves the metadata's own (``distribution_for``: the first
    provider of the top-level import name), so changing that rule changes both
    at once. Datasets, models and tokenizers have no distribution and are always
    kept.
    """
    if resource_type != ResourceType.PACKAGE:
        return True
    return bool(distribution_for(name.partition(":")[2], inventory.provided))


def build_catalogue(
    *,
    flavor: str,
    alpha: float,
    metadata: Metadata,
    entries: Sequence[tuple[str, CheckpointPlan]],
    inventory: Inventory | None,
) -> dict[str, Any]:
    """Assemble the catalogue document.

    ``entries`` pairs each checkpoint id with the plan it was built from.
    ``inventory`` is what the rootfs provides; anything the metadata measured
    that it does not provide is left out. ``None`` means the rootfs could not be
    listed, so nothing is left out and ``distributions`` is empty.
    """
    # Only measured entries can be absent from the rootfs. A name the metadata
    # never measured (a standard-library module in a dependency list, say) is
    # not this function's to judge and passes through untouched.
    dropped = (
        set()
        if inventory is None
        else {
            name
            for name, facts in metadata.resources.items()
            if not _in_rootfs(name, facts.resource_type, inventory)
        }
    )

    items: dict[str, dict[str, Any]] = {}
    for name, facts in metadata.resources.items():
        if name in dropped:
            continue
        entry: dict[str, Any] = {
            "size_mb": round(facts.memory_size_mb, 4),
            "load_time": facts.import_time,
            "resource_type": str(facts.resource_type),
            "distribution": facts.distribution,
        }
        dependencies = sorted(metadata.direct_dependencies(name) - dropped)
        if dependencies:
            entry["dependencies"] = dependencies
        items[name] = entry

    checkpoints = []
    for checkpoint_id, plan in entries:
        keys = [k for k in _checkpoint_items(plan, metadata) if k not in dropped]
        canonical = [k for k in _canonical_items(plan) if k not in dropped]
        size_mb = sum(metadata.memory_size_mb(key) for key in keys)
        checkpoints.append(
            {
                "id": checkpoint_id,
                "items": keys,
                "canonical": canonical,
                # Raw total item size.
                "size_mb": round(size_mb, 4),
            }
        )

    return {
        "version": CATALOGUE_VERSION,
        "flavor": flavor,
        "alpha": alpha,
        "distributions": _distribution_versions(inventory),
        "items": items,
        "checkpoints": checkpoints,
    }


def _distribution_versions(inventory: Inventory | None) -> dict[str, str]:
    """Every distribution the rootfs has, mapped to its version (``""`` if unknown).

    A name is always listed when the rootfs has it, even without a version, because
    the router leaves a call's package out whenever the name is a key.
    """
    if inventory is None:
        return {}
    return {name: inventory.versions.get(name, "") for name in sorted(inventory.distributions)}


def write_catalogue(path: Path, document: dict[str, Any]) -> None:
    """Write the catalogue atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    partial.replace(path)
