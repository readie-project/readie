"""Guard tests: properties of the package that reviews miss.

These fail loudly when a later change quietly breaks an architectural rule, and
each one corresponds to something that was actually wrong before.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterator
from types import ModuleType

import pytest

import readie
from readie import protocol
from readie._proto import resources_pb2
from readie.budget import ResourceKind
from readie.client import Client
from readie.codec import CloudpickleCodec, ResultCodec
from readie.transport import AsyncGrpcTransport, AsyncTransport, GrpcTransport, Transport

PACKAGE = "readie"


def modules() -> Iterator[ModuleType]:
    """Every importable module in the package except the generated stubs."""
    package = importlib.import_module(PACKAGE)
    for info in pkgutil.walk_packages(package.__path__, prefix=f"{PACKAGE}."):
        if "._proto" in info.name:
            continue
        yield importlib.import_module(info.name)


def test_the_public_surface_is_exactly_what_all_declares() -> None:
    # Submodule names bound by `from readie.x import y` are not public surface,
    # and neither is the `annotations` future import.
    hidden = {m.__name__.rsplit(".", 1)[1] for m in modules()} | {"annotations"}
    exported = {name for name in dir(readie) if not name.startswith("_") or name == "__version__"}
    assert exported - hidden == set(readie.__all__)


def test_everything_in_all_actually_exists() -> None:
    for name in readie.__all__:
        assert hasattr(readie, name), name


def test_all_is_sorted() -> None:
    assert readie.__all__ == sorted(readie.__all__)


def test_the_only_module_level_mutable_state_is_the_documented_default_client() -> None:
    # `_default` in readie.decorator is deliberate and documented. Anything else
    # holding a Client, a channel or a transport at module scope is a bug: it
    # makes the package's behaviour depend on import order.
    forbidden = (Client, GrpcTransport, AsyncGrpcTransport)
    offenders = []
    for module in modules():
        for name, value in vars(module).items():
            if isinstance(value, forbidden) and (module.__name__, name) != (
                "readie.decorator",
                "_default",
            ):
                offenders.append(f"{module.__name__}.{name}")
    assert offenders == []


def test_protocol_never_imports_grpc() -> None:
    # This is what keeps the wire protocol unit-testable with no server, no
    # channel and no event loop.
    source = inspect.getsource(protocol)
    assert "import grpc" not in source
    assert "grpc" not in {name for name in vars(protocol) if not name.startswith("_")}


def test_resources_never_imports_protobuf() -> None:
    # Import extraction is pure source analysis; keeping protobuf out of it is
    # what lets its tests run with no generated stubs and no wire types.
    from readie import resources

    assert "_proto" not in inspect.getsource(resources)


def test_resource_kind_matches_the_proto_enum() -> None:
    # Budget.kind is sent to the wire by value, so the client enum and the proto
    # enum must agree number-for-number or a memory budget arrives as GPU memory.
    assert int(ResourceKind.MEMORY) == int(resources_pb2.RESOURCE_KIND_MEMORY)
    assert int(ResourceKind.GPU_MEMORY) == int(resources_pb2.RESOURCE_KIND_GPU_MEMORY)


@pytest.mark.parametrize(
    ("implementation", "seam"),
    [
        (CloudpickleCodec, ResultCodec),
        (GrpcTransport, Transport),
        (AsyncGrpcTransport, AsyncTransport),
    ],
)
def test_every_implementation_satisfies_its_protocol(implementation: type, seam: type) -> None:
    assert issubclass(implementation, seam)


def test_the_async_transport_seam_is_actually_awaitable() -> None:
    assert inspect.iscoroutinefunction(AsyncGrpcTransport.execute)
    assert not inspect.iscoroutinefunction(GrpcTransport.execute)


def test_importing_readie_opens_no_connection_and_needs_no_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A module of @remote definitions must import as fast as one without them.
    monkeypatch.delenv("READIE_ROUTER_URI", raising=False)
    module = importlib.reload(importlib.import_module("readie.decorator"))
    assert module._default is None


def test_every_module_has_a_docstring() -> None:
    for module in modules():
        assert module.__doc__, module.__name__


def test_every_public_error_descends_from_the_base() -> None:
    for name in readie.__all__:
        value = getattr(readie, name)
        if (
            isinstance(value, type)
            and issubclass(value, Exception)
            and not issubclass(value, Warning)
        ):
            assert issubclass(value, readie.ReadieError), name
