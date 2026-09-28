"""The checkpoint catalogue, and request-time selection from it.

Loaded once at startup from the per-flavor ``catalogue.json`` files the pipeline
writes (one per generation). Selection picks the checkpoint minimising

    alpha * size(checkpoint)  +  Σ load_time(closure of required items, minus
                                              what the checkpoint already has)

against a cold start (no checkpoint at all), so a checkpoint is chosen only when
it saves more import time than its size costs. This is the dual of the planner's
objective and uses the same ``alpha`` (applied from the catalogue's ``alpha``,
so the trade can be retuned at the router).

"Closure" because a required item's own measured load time assumes its
dependencies are already resident (see the pipeline's ``metadata/analyze.py``);
a request for it genuinely needs whichever of those dependencies this
checkpoint does not already carry, so they are priced too -- each one once, no
matter how many required items share it.

Nothing here imports gRPC or awaits: it is pure domain, like the rest of
``scheduling/``. Absent or unreadable catalogues yield an empty mapping, which
means cold starts -- the behaviour before request-time selection existed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

#: On-disk format version; must match the pipeline's ``catalogue.CATALOGUE_VERSION``.
CATALOGUE_VERSION = 1


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    """One checkpoint's contents and its precomputed size term (alpha * size)."""

    checkpoint_id: str
    items: frozenset[str]
    size: float


@dataclass(frozen=True, slots=True)
class Catalogue:
    """The checkpoints of one generation, and the cost to reach any item."""

    flavor: str = ""
    load_times: Mapping[str, float] = field(default_factory=dict)
    dependencies: Mapping[str, frozenset[str]] = field(default_factory=dict)
    checkpoints: tuple[_Checkpoint, ...] = ()

    def closure(self, required: Iterable[str]) -> frozenset[str]:
        """``required``, plus everything its items transitively depend on.

        A dependency reachable from more than one required item is walked
        once, via ``seen`` -- so a shared dependency is priced once below, not
        once per item that needs it.
        """
        seen: set[str] = set()
        stack = list(required)
        while stack:
            item = stack.pop()
            if item in seen:
                continue
            seen.add(item)
            stack.extend(self.dependencies.get(item, frozenset()) - seen)
        return frozenset(seen)

    def select(self, required: Iterable[str]) -> str:
        """Return the id of the cheapest checkpoint, or ``""`` for a cold start.

        Only items the catalogue has measured enter the cost; an unknown
        required item is in no checkpoint and so adds the same amount to every
        option, cold start included, and cannot change the winner.
        """
        priced = {
            item: self.load_times[item]
            for item in self.closure(required)
            if item in self.load_times
        }
        full_residual = sum(priced.values())

        best_id, best_cost = "", full_residual  # the cold-start baseline
        for checkpoint in self.checkpoints:
            saved = sum(t for item, t in priced.items() if item in checkpoint.items)
            cost = checkpoint.size + (full_residual - saved)
            if cost <= best_cost:
                best_id, best_cost = checkpoint.checkpoint_id, cost
        return best_id

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> Catalogue:
        """Build a catalogue from a parsed ``catalogue.json`` document."""
        raw_items = document.get("items")
        alpha = float(document.get("alpha", 0.005))  # type: ignore[arg-type]  # JSON value, narrowed at runtime by float()
        items = raw_items if isinstance(raw_items, Mapping) else {}
        load_times = {
            str(key): float(entry.get("load_time", 0.0))
            for key, entry in items.items()
            if isinstance(entry, Mapping)
        }
        dependencies = {
            str(key): frozenset(str(dep) for dep in entry.get("dependencies", ()))
            for key, entry in items.items()
            if isinstance(entry, Mapping) and entry.get("dependencies")
        }

        raw_checkpoints = document.get("checkpoints")
        entries = raw_checkpoints if isinstance(raw_checkpoints, list) else []
        checkpoints = tuple(
            _Checkpoint(
                checkpoint_id=str(entry["id"]),
                items=frozenset(entry.get("items", ())),
                size=alpha * float(entry.get("size_mb", 0.0)),
            )
            for entry in entries
            if isinstance(entry, Mapping)
        )
        return cls(
            flavor=str(document.get("flavor", "")),
            load_times=load_times,
            dependencies=dependencies,
            checkpoints=checkpoints,
        )


def load_catalogues(
    directory: Path | None,
    *,
    warn: Callable[[str, str], None] | None = None,
) -> dict[str, Catalogue]:
    """Load every ``*.json`` catalogue in ``directory``, keyed by its flavor.

    A missing directory or a bad file is tolerated: the worst case is a cold
    start, never a router that will not boot. ``warn(path, reason)`` is called
    for each file that is skipped.
    """
    catalogues: dict[str, Catalogue] = {}
    if directory is None or not directory.is_dir():
        return catalogues

    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            if warn is not None:
                warn(str(path), f"unreadable: {exc}")
            continue
        if not isinstance(document, dict) or document.get("version") != CATALOGUE_VERSION:
            if warn is not None:
                version = document.get("version") if isinstance(document, dict) else "?"
                warn(str(path), f"unsupported catalogue version {version}")
            continue
        catalogue = Catalogue.from_document(document)
        catalogues[catalogue.flavor] = catalogue
    return catalogues
