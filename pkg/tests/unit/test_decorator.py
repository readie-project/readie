"""The decorator and the callable it produces."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

import crfs
from crfs.client import Client
from crfs.config import Settings
from crfs.decorator import RemoteFunction, remote
from crfs.resources import NullEstimator
from tests.fakes.transport import AsyncRecordingTransport, RecordingTransport


@dataclass
class Rig:
    """A client plus the fake it was built with, so tests can assert on both."""

    client: Client
    sent: RecordingTransport


def build(result: object = "remote-result", uri: str = "127.0.0.1:1") -> Rig:
    sent = RecordingTransport(result)
    return Rig(
        Client(
            Settings(router_uri=uri),
            transport=sent,
            async_transport=AsyncRecordingTransport(result),
            estimator=NullEstimator(),
        ),
        sent,
    )


@pytest.fixture
def rig() -> Rig:
    return build()


@pytest.fixture(autouse=True)
def _no_leaked_default() -> Iterator[None]:
    crfs.reset()
    yield
    crfs.reset()


def test_the_bare_form_produces_a_remote_function(rig: Rig) -> None:
    @remote(client=rig.client)
    def f(a, b):
        return a + b

    assert isinstance(f, RemoteFunction)
    assert f(1, 2) == "remote-result"


def test_the_bare_decorator_takes_no_parentheses() -> None:
    @remote
    def f():
        return 1

    assert isinstance(f, RemoteFunction)


def test_metadata_survives_decoration() -> None:
    @remote
    def documented(a: int) -> int:
        """A docstring."""
        return a

    assert documented.__name__ == "documented"
    assert documented.__doc__ == "A docstring."
    assert documented.__wrapped__ is documented.func


def test_local_runs_here_and_skips_the_transport(rig: Rig) -> None:
    @remote(client=rig.client)
    def f(a, b):
        return a + b

    assert f.local(2, 3) == 5
    assert rig.sent.calls == []


async def test_aio_runs_on_the_loop(rig: Rig) -> None:
    @remote(client=rig.client)
    def f():
        return None

    assert await f.aio() == "remote-result"


async def test_calling_the_blocking_form_from_a_loop_raises_rather_than_hanging(
    rig: Rig,
) -> None:
    @remote(client=rig.client)
    def f():
        return None

    with pytest.raises(crfs.BlockingCallInEventLoopError):
        f()


def test_bind_returns_a_copy_and_does_not_mutate_the_shared_decoration(rig: Rig) -> None:
    @remote(client=rig.client)
    def f():
        return None

    session = crfs.Session("sess-x")
    bound = f.bind(session)

    assert bound is not f
    bound()
    f()

    sessions = [call[0].session_id for call in rig.sent.calls]
    assert sessions[0] == "sess-x"
    assert sessions[1] != "sess-x"


def test_bind_can_override_the_timeout_and_client(rig: Rig) -> None:
    @remote(client=rig.client)
    def f():
        return None

    other = build(result="other", uri="h:2")
    assert f.bind(client=other.client, timeout=9.0)() == "other"
    assert other.sent.last[3] == 9.0


def test_a_decorator_timeout_is_applied(rig: Rig) -> None:
    @remote(client=rig.client, timeout=4.0)
    def f():
        return None

    f()
    assert rig.sent.last[3] == 4.0


def test_decorating_a_method_still_receives_self(rig: Rig) -> None:
    class Service:
        def __init__(self, base):
            self.base = base

        @remote(client=rig.client)
        def scaled(self, factor):
            return self.base * factor

    # The descriptor binds self; local() proves the argument arrived.
    assert Service(3).scaled(4) == "remote-result"
    import cloudpickle

    loaded = cloudpickle.loads(rig.sent.last[1])
    assert loaded["func"](*loaded["args"]) == 12


def test_repr_names_the_function_and_any_session(rig: Rig) -> None:
    @remote(client=rig.client)
    def f():
        return None

    assert "f" in repr(f)
    assert "sess-y" in repr(f.bind(crfs.Session("sess-y")))


# ---------------------------------------------------------------------------
# The process-wide default
# ---------------------------------------------------------------------------
def test_configure_installs_a_client_the_decorator_picks_up() -> None:
    installed = crfs.configure(router_uri="somewhere:1234")
    assert crfs.default_client() is installed
    assert installed.settings.router_uri == "somewhere:1234"


def test_configure_rejects_a_client_and_settings_together(rig: Rig) -> None:
    with pytest.raises(TypeError, match="not both"):
        crfs.configure(rig.client, router_uri="x:1")


def test_replacing_the_default_closes_the_previous_one() -> None:
    first = crfs.configure(router_uri="a:1")
    crfs.configure(router_uri="b:2")
    assert first._closed


def test_the_default_is_created_lazily_and_never_at_import() -> None:
    import crfs.decorator as module

    assert module._default is None
    crfs.default_client()
    assert module._default is not None


def test_an_explicit_client_never_consults_the_default(rig: Rig) -> None:
    @remote(client=rig.client)
    def f():
        return None

    f()
    import crfs.decorator as module

    assert module._default is None


def test_reset_is_idempotent() -> None:
    crfs.reset()
    crfs.reset()
    assert asyncio.iscoroutinefunction(crfs.Client.acall)
