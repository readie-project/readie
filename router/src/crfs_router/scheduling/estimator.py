"""Turning a client's resource estimate into an allocation.

A ``Protocol`` rather than a base class because the real implementation is a
prediction model living in a *different repository*
(``python-execution-memory-prediction``, per the project README). That model
must be able to satisfy this interface without importing the router.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from crfs_router.scheduling.models import Demand


@dataclass(frozen=True, slots=True)
class Estimate:
    """The client's description of what it is about to run.

    A plain value rather than the protobuf message, so the estimator seam does
    not force an implementation to depend on generated code.
    """

    code: str = ""
    imports: tuple[str, ...] = ()
    variables: tuple[str, ...] = ()


class ResourceEstimator(Protocol):
    """Predicts what a request will need."""

    def estimate(self, estimate: Estimate) -> Demand:
        """Return the allocation to request from a worker."""
        ...


@dataclass(frozen=True, slots=True)
class StaticEstimator:
    """Gives every request the same budget.

    Stands in until the prediction model lands. It still forwards the imports,
    so the worker receives a real resource list even though the allocation is
    fixed — which is more than the previous router did, having dropped the
    estimate entirely.
    """

    cpu_alloc: int = 512 * 1024 * 1024
    gpu_alloc: int = 0

    def estimate(self, estimate: Estimate) -> Demand:
        """Return the fixed allocation, carrying the client's imports through."""
        return Demand(
            cpu_alloc=self.cpu_alloc,
            gpu_alloc=self.gpu_alloc,
            resources=estimate.imports,
        )
