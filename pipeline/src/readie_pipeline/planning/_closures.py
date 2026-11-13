"""Matrix helpers shared by the planners that select whole closures."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def membership(closures: Sequence[frozenset[str]]) -> tuple[np.ndarray, list[str]]:
    """``members[n, p]`` is 1 when closure ``n`` holds package ``p``; also the sorted packages."""
    packages = sorted({name for closure in closures for name in closure})
    column = {name: i for i, name in enumerate(packages)}
    members = np.zeros((len(closures), len(packages)), dtype=float)
    for row, closure in enumerate(closures):
        members[row, [column[name] for name in closure]] = 1.0
    return members, packages


def jaccard_distances(closures: Sequence[frozenset[str]]) -> np.ndarray:
    """``1 - |a & b| / |a | b|`` for every pair, as one matrix product.

    Closures are non-empty (see ``GreedyPlanner._nodes``), so no union is empty.
    """
    members, _ = membership(closures)
    sizes = members.sum(axis=1)
    shared = members @ members.T
    union = sizes[:, None] + sizes[None, :] - shared
    dist: np.ndarray = 1.0 - shared / union
    return dist


def order_by_weight(chosen: Sequence[int], assigned_weight: Sequence[float]) -> list[int]:
    """``chosen`` heaviest first, ties on the lower index, so a prefix is the best subset."""
    return [
        index
        for index, _ in sorted(
            zip(chosen, assigned_weight, strict=True), key=lambda pair: (-pair[1], pair[0])
        )
    ]
