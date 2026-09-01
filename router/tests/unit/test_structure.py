"""Architectural invariants, enforced rather than documented.

Each of these encodes a property the design depends on and that a reviewer
would not reliably catch. They are cheap; the bugs they prevent are not.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from types import ModuleType

import pytest

import readie_router
from readie_router.proto import registry_pb2, resources_pb2
from readie_router.scheduling import models
from readie_router.scheduling.scheduler import Scheduler
from readie_router.scheduling.state import ClusterState
from readie_router.workers.channels import WorkerChannelPool


def _modules() -> list[ModuleType]:
    """Import every module in the package except the generated stubs."""
    found = []
    for info in pkgutil.walk_packages(readie_router.__path__, "readie_router."):
        if info.name.startswith("readie_router.proto"):
            continue
        found.append(importlib.import_module(info.name))
    return found


def test_no_module_level_mutable_state() -> None:
    """A module-level singleton is how the old router could end up with two.

    `router/` had no `__init__.py`, so `scheduler` was importable under more
    than one module name, each with its own `Scheduler()` — two registries,
    each seeing half the cluster.
    """
    banned = (ClusterState, Scheduler, WorkerChannelPool)
    offenders = [
        f"{module.__name__}.{name}"
        for module in _modules()
        for name, value in vars(module).items()
        if isinstance(value, banned)
    ]
    assert not offenders, f"module-level mutable state: {offenders}"


@pytest.mark.parametrize("cls", [ClusterState, Scheduler])
def test_the_domain_never_awaits(cls: type) -> None:
    """Atomicity comes from these methods never yielding to the event loop.

    On a single-threaded loop a synchronous method runs to completion with no
    interleaving, which is why the scheduler needs no locks. An `async def`
    here silently reintroduces the read-modify-write race that
    `provision`/`bind` were written to eliminate.
    """
    for name, member in inspect.getmembers(cls, inspect.isfunction):
        assert not inspect.iscoroutinefunction(member), f"{cls.__name__}.{name} is async"
        assert not inspect.isasyncgenfunction(member), f"{cls.__name__}.{name} is an async gen"


def test_the_domain_does_not_import_grpc() -> None:
    """`scheduling/` must stay testable with no event loop and no transport."""
    for info in pkgutil.walk_packages(readie_router.__path__, "readie_router."):
        if not info.name.startswith("readie_router.scheduling"):
            continue
        source = inspect.getsource(importlib.import_module(info.name))
        assert "import grpc" not in source, f"{info.name} imports grpc"
        assert "grpcserver" not in source, f"{info.name} depends on the transport layer"


def test_status_constants_match_the_wire_enum() -> None:
    """The domain redeclares these so it need not import generated code.

    Note the gap at 1: the wire enum has no value 1, and a "tidied" contiguous
    enum here would silently mis-map every status.
    """
    wire = {v.name: v.number for v in registry_pb2.DESCRIPTOR.enum_types_by_name["Status"].values}
    assert wire == {
        "STATUS_UNKNOWN": models.STATUS_UNKNOWN,
        "STATUS_READY": models.STATUS_READY,
        "STATUS_BUSY": models.STATUS_BUSY,
        "STATUS_ERROR": models.STATUS_ERROR,
        "STATUS_REMOVED": models.STATUS_REMOVED,
    }
    assert 1 not in wire.values(), "the wire enum's gap at 1 is part of the contract"


def test_resource_kind_constants_match_the_wire_enum() -> None:
    """Resource kinds are redeclared and must track the wire enum by value.

    A placement forwards them to the worker by number, so a mismatch would send
    a memory budget as GPU memory.
    """
    assert models.RESOURCE_MEMORY == resources_pb2.RESOURCE_KIND_MEMORY
    assert models.RESOURCE_GPU_MEMORY == resources_pb2.RESOURCE_KIND_GPU_MEMORY
