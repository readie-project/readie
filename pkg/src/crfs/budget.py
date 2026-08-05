"""Per-function resource budgets.

A budget is ``(kind, alloc, max)`` in bytes: what a container starts with and how
far the worker may grow it. The decorator exposes one pair of keywords per kind
(``memory``/``max_memory``, ``gpu_memory``/``max_gpu_memory``); a new dimension
is one row in ``_KWARG_KINDS`` here plus a capacity source in the router and, if
the runtime can enforce it, a grower in the worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from crfs._memory import parse_memory
from crfs.errors import ConfigurationError


class ResourceKind(IntEnum):
    """Kinds of resource a budget can cover.

    The values mirror ``ResourceKind`` in ``protos/resources.proto`` and a test
    asserts they stay in step, so a budget converts to the wire enum by value.
    """

    MEMORY = 1
    GPU_MEMORY = 2


@dataclass(frozen=True, slots=True)
class Budget:
    """One resource budget in bytes. ``alloc``/``max`` of 0 mean "use the default"."""

    kind: ResourceKind
    alloc: int = 0
    max: int = 0


# (initial keyword, ceiling keyword, kind). Extend to add a dimension.
_KWARG_KINDS: tuple[tuple[str, str, ResourceKind], ...] = (
    ("memory", "max_memory", ResourceKind.MEMORY),
    ("gpu_memory", "max_gpu_memory", ResourceKind.GPU_MEMORY),
)


def build_budgets(
    *,
    memory: str | int | None = None,
    max_memory: str | int | None = None,
    gpu_memory: str | int | None = None,
    max_gpu_memory: str | int | None = None,
) -> tuple[Budget, ...]:
    """Turn the decorator's size keywords into budgets, validating each.

    A kind with neither value set is omitted entirely, so the router applies its
    own default for it. A ceiling below its initial is a contradiction and fails
    here rather than confusing the scheduler.
    """
    values = {
        "memory": memory,
        "max_memory": max_memory,
        "gpu_memory": gpu_memory,
        "max_gpu_memory": max_gpu_memory,
    }
    budgets: list[Budget] = []
    for alloc_kw, max_kw, kind in _KWARG_KINDS:
        alloc = parse_memory(values[alloc_kw])
        ceiling = parse_memory(values[max_kw])
        if alloc == 0 and ceiling == 0:
            continue
        if alloc and ceiling and ceiling < alloc:
            msg = f"{max_kw} ({ceiling}) must be at least {alloc_kw} ({alloc})"
            raise ConfigurationError(msg)
        budgets.append(Budget(kind=kind, alloc=alloc, max=ceiling))
    return tuple(budgets)
