"""The checkpoint catalogue the router selects from.

Written once per generation beside the manifest and read by the router at
startup. It is the whole input to request-time checkpoint selection:

* ``items`` - every measured item (package, dataset, model, tokenizer) with its
  disk size and load time, so the router can price the residual load of anything
  a request needs.
* ``checkpoints`` - each checkpoint's item set and its raw total size (MB).

The router picks the checkpoint minimising ``alpha * size + Σ load_time(required
items not in it)``, applying its own ``alpha`` to the raw size. The ``alpha`` the
planner built these under is recorded here for reference.

Items are keyed the way the client names them in a request's required set:
packages by their bare import name, and datasets/models/tokenizers prefixed with
their kind (``model:gpt2``), so the router can match a request's needs directly.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from readie_pipeline.metadata.models import Metadata, ResourceType
from readie_pipeline.planning.ports import CheckpointPlan

#: The on-disk format version, so a router can refuse a catalogue it cannot read.
CATALOGUE_VERSION = 1


def item_key(name: str, resource_type: ResourceType | str) -> str:
    """Return an item's catalogue key.

    Packages are keyed by their bare name; datasets, models and tokenizers are
    prefixed with their kind (``model:gpt2``), matching how a request names them.
    """
    kind = str(resource_type)
    if kind == ResourceType.PACKAGE:
        return name
    return f"{kind}:{name}"


def _checkpoint_items(plan: CheckpointPlan) -> list[str]:
    """Every item a checkpoint contains, keyed for the catalogue."""
    keys = list(plan.imports)
    keys += [item_key(n, ResourceType.DATASET) for n in plan.datasets]
    keys += [item_key(n, ResourceType.MODEL) for n in plan.models]
    keys += [item_key(n, ResourceType.TOKENIZER) for n in plan.tokenizers]
    return sorted(keys)


def build_catalogue(
    *,
    flavor: str,
    alpha: float,
    metadata: Metadata,
    entries: Sequence[tuple[str, CheckpointPlan]],
) -> dict[str, Any]:
    """Assemble the catalogue document.

    ``entries`` pairs each checkpoint id with the plan it was built from.
    """
    items: dict[str, dict[str, Any]] = {}
    for name, facts in metadata.packages.items():
        items[item_key(name, facts.resource_type)] = {
            "size_mb": round(facts.disk_size_mb, 4),
            "load_time": facts.import_time,
            "resource_type": str(facts.resource_type),
        }

    checkpoints = []
    for checkpoint_id, plan in entries:
        keys = _checkpoint_items(plan)
        size_mb = sum(metadata.size_mb(_bare(key)) for key in keys)
        checkpoints.append(
            {
                "id": checkpoint_id,
                "items": keys,
                # Raw total item size.
                "size_mb": round(size_mb, 4),
            }
        )

    return {
        "version": CATALOGUE_VERSION,
        "flavor": flavor,
        "alpha": alpha,
        "items": items,
        "checkpoints": checkpoints,
    }


def _bare(key: str) -> str:
    """Strip a ``kind:`` prefix to recover the metadata lookup name."""
    _, sep, rest = key.partition(":")
    return rest if sep else key


def write_catalogue(path: Path, document: dict[str, Any]) -> None:
    """Write the catalogue atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    partial.replace(path)
