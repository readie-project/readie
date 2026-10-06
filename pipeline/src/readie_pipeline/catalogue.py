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
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
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


def _keyed_items(packages: Iterable[str], plan: CheckpointPlan) -> list[str]:
    """Keys a package set plus a plan's non-package resources for the catalogue."""
    keys = list(packages)
    keys += [item_key(n, ResourceType.DATASET) for n in plan.datasets]
    keys += [item_key(n, ResourceType.MODEL) for n in plan.models]
    keys += [item_key(n, ResourceType.TOKENIZER) for n in plan.tokenizers]
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
    return _keyed_items(metadata.closure(plan.imports), plan)


def _canonical_items(plan: CheckpointPlan) -> list[str]:
    """The packages some request actually asked for, keyed for the catalogue.

    Not expanded through dependencies -- this is the same list the executor
    was told to import (``plan.imports``), kept alongside the full closure
    (``_checkpoint_items``) for a reader that wants to know what was actually
    requested rather than what pricing needed.
    """
    return _keyed_items(plan.imports, plan)


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
        entry: dict[str, Any] = {
            "size_mb": round(facts.memory_size_mb, 4),
            "load_time": facts.import_time,
            "resource_type": str(facts.resource_type),
        }
        dependencies = metadata.direct_dependencies(name)
        if dependencies:
            entry["dependencies"] = sorted(dependencies)
        items[item_key(name, facts.resource_type)] = entry

    checkpoints = []
    for checkpoint_id, plan in entries:
        keys = _checkpoint_items(plan, metadata)
        size_mb = sum(metadata.memory_size_mb(_bare(key)) for key in keys)
        checkpoints.append(
            {
                "id": checkpoint_id,
                "items": keys,
                "canonical": _canonical_items(plan),
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
