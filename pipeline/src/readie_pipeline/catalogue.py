"""The checkpoint catalogue the router selects from.

Written once per generation beside the manifest and read by the router at
startup. It is the whole input to request-time checkpoint selection:

* ``items`` - every measured item (package, dataset, model, tokenizer) with its
  resident memory size, load time, and (for a package) the item keys of
  ``dependencies``, so the router can expand a request's declared needs into
  the same closure the planner scored checkpoints against before pricing the
  residual. Despite the name, this is now already the *full* transitive
  closure, not just first-level requirements: ``Metadata.direct_dependencies``
  is empirically measured (a ``sys.modules`` diff around the actual import),
  and that diff is already fully transitive in one shot -- the router's own
  recursive expansion over it is still correct, just redundant now. ``size_mb``
  here is memory, not disk: what gVisor actually copies back on restore, and
  what the ``alpha * size`` term below is charging for -- see
  ``PackageFacts.memory_size_mb``.
* ``checkpoints`` - each checkpoint's item set and its raw total size (MB). Two
  item lists per checkpoint: ``items`` is the full dependency closure (what is
  actually resident, and what pricing needs), ``canonical`` is only what some
  request actually asked for (what the executor was told to import; see
  ``planning/greedy.py``'s ``_describe``).

The router picks the checkpoint minimising ``alpha * size + Σ load_time(closure
of required items not in it)``, applying the catalogue's ``alpha`` to the raw size. The
``alpha`` recorded here is the one measured after capture; the router has no
``alpha`` setting of its own.

Items are keyed the way the client names them in a request's required set:
packages by their resolved dotted import name (not necessarily top-level --
``sklearn`` and ``sklearn.svm`` are independent items), and
datasets/models/tokenizers prefixed with their kind (``model:gpt2``), so the
router can match a request's needs directly.

Only what the rootfs actually carries is ported from the metadata. The metadata
may have been measured in a different image than the one the sandbox runs, so
``build_catalogue`` takes an ``Inventory`` of the rootfs and drops every package
it has no distribution for, along with references to it. The
inventory is also written as the top-level ``distributions`` object: canonical
name -> installed version, sorted by name. The router leaves out of a request's
``packages`` anything whose name is a key, and reports the version when it does.
The versions are those of the rootfs as it was built, not of the metadata, and an
empty string means the rootfs has the distribution but no version for it. Each item records
its own ``distribution`` (blank for datasets, models and tokenizers, which have
none).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from readie_pipeline.metadata.inventory import Inventory, distribution_for
from readie_pipeline.metadata.models import Metadata, ResourceType
from readie_pipeline.planning.ports import CheckpointPlan

#: The on-disk format version, so a router can refuse a catalogue it cannot read.
CATALOGUE_VERSION = 2


def item_key(name: str, resource_type: ResourceType | str) -> str:
    """Return an item's catalogue key.

    Packages are keyed by their bare name; datasets, models and tokenizers are
    prefixed with their kind (``model:gpt2``), matching how a request names them.
    """
    kind = str(resource_type)
    if name.startswith(f"{kind}:"):
        return name
    if kind == ResourceType.PACKAGE:
        return name
    return f"{kind}:{name}"


def _resource_item_key(
    name: str,
    resource_type: ResourceType,
    metadata: Metadata,
) -> str:
    """Use a measured source-qualified type when one is available."""
    if ":" in name:
        return name
    candidates = {
        ResourceType.DATASET: (ResourceType.KAGGLE_DATASET, ResourceType.HF_DATASET),
        ResourceType.MODEL: (ResourceType.HF_MODEL, ResourceType.MODEL),
        ResourceType.TOKENIZER: (ResourceType.HF_TOKENIZER, ResourceType.TOKENIZER),
    }.get(resource_type, (resource_type,))
    for candidate in candidates:
        if f"{candidate}:{name}" in metadata.packages:
            return item_key(name, candidate)
    return item_key(name, resource_type)


def _keyed_items(
    packages: Iterable[str],
    plan: CheckpointPlan,
    metadata: Metadata,
) -> list[str]:
    """Keys a package set plus a plan's non-package resources for the catalogue."""
    keys = list(packages)
    keys += [_resource_item_key(n, ResourceType.DATASET, metadata) for n in plan.datasets]
    keys += [_resource_item_key(n, ResourceType.MODEL, metadata) for n in plan.models]
    keys += [_resource_item_key(n, ResourceType.TOKENIZER, metadata) for n in plan.tokenizers]
    return sorted(keys)


def _checkpoint_items(plan: CheckpointPlan, metadata: Metadata) -> list[str]:
    """Every item a checkpoint actually holds resident, keyed for the catalogue.

    ``plan.imports`` is deliberately just what some request actually asked
    for -- what the executor is told to import, since Python's own import
    machinery pulls in the rest -- not the full dependency closure the
    planner scored it against. This reconstructs that closure: a request-time
    residual has to be priced against everything actually resident in this
    checkpoint, dependencies included, not just the trimmed executor-facing
    list (see ``_canonical_items`` for that one).
    """
    return _keyed_items(metadata.closure(plan.imports), plan, metadata)


def _canonical_items(plan: CheckpointPlan, metadata: Metadata) -> list[str]:
    """The packages some request actually asked for, keyed for the catalogue.

    Not expanded through dependencies -- this is the same list the executor
    was told to import (``plan.imports``), kept alongside the full closure
    (``_checkpoint_items``) for a reader that wants to know what was actually
    requested rather than what pricing needed.
    """
    return _keyed_items(plan.imports, plan, metadata)


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
    return bool(distribution_for(name, inventory.provided))


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
            for name, facts in metadata.packages.items()
            if not _in_rootfs(name, facts.resource_type, inventory)
        }
    )

    items: dict[str, dict[str, Any]] = {}
    for name, facts in metadata.packages.items():
        if name in dropped:
            continue
        bare_name = name.partition(":")[2] if ":" in name else name
        entry: dict[str, Any] = {
            "size_mb": round(facts.memory_size_mb, 4),
            "load_time": facts.import_time,
            "resource_type": str(facts.resource_type),
            "distribution": facts.distribution,
        }
        dependencies = sorted(metadata.direct_dependencies(bare_name) - dropped)
        if dependencies:
            entry["dependencies"] = dependencies
        items[item_key(bare_name, facts.resource_type)] = entry

    checkpoints = []
    for checkpoint_id, plan in entries:
        keys = [k for k in _checkpoint_items(plan, metadata) if k not in dropped]
        canonical = [k for k in _canonical_items(plan, metadata) if k not in dropped]
        size_mb = sum(_memory_size(key, metadata) for key in keys)
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


def _bare(key: str) -> str:
    """Strip a ``kind:`` prefix to recover the metadata lookup name."""
    _, sep, rest = key.partition(":")
    return rest if sep else key


def _memory_size(key: str, metadata: Metadata) -> float:
    """Look up the measured size using either a typed or legacy metadata key."""
    facts = metadata.packages.get(key) or metadata.packages.get(_bare(key))
    return facts.memory_size_mb if facts is not None else 0.0


def write_catalogue(path: Path, document: dict[str, Any]) -> None:
    """Write the catalogue atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    partial.replace(path)
