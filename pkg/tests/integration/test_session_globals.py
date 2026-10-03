"""Global persistence through the SDK against an explicitly selected real stack.

The ordinary gRPC fixture executes code in a fake router, so it cannot establish
sandbox persistence. Set READIE_TEST_ROUTER_URI to opt in to these tests; no fake
router, .local execution, or automatic repair is used here.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from textwrap import dedent
from types import FunctionType
from typing import Any

import pytest

from readie import IgnoredGlobalsWarning, RemoteExecutionError, Session, remote
from readie.client import Client
from readie.config import Settings

pytestmark = pytest.mark.real_stack

StackCall = Callable[[str, Session | None], Any]


@pytest.fixture(params=[False, True], ids=["checkpoint-enabled", "cold-start"])
def stack_call(request: pytest.FixtureRequest) -> Iterator[StackCall]:
    uri = os.environ.get("READIE_TEST_ROUTER_URI")
    if not uri:
        pytest.skip("set READIE_TEST_ROUTER_URI to run against a real Readie stack")
    tls = os.environ.get("READIE_TEST_TLS", "false").lower() in {"true", "1", "yes"}
    settings = Settings(router_uri=uri, tls=tls, timeout=120.0, stream_logs=False)
    disable_optimized = bool(request.param)

    with Client(settings) as client:

        def invoke(source: str, session: Session | None) -> Any:
            # An unimportable, fresh namespace forces by-value serialization.
            # A reader's client namespace contains no value from earlier calls.
            namespace: dict[str, object] = {"__name__": "__readie_session_globals_test__"}
            exec(dedent(source), namespace)  # noqa: S102 - trusted, handwritten workloads
            func = namespace["task"]
            assert isinstance(func, FunctionType)
            wrapped = remote(
                client=client,
                memory="256Mi",
                disable_optimized_execution=disable_optimized,
            )(func)
            return wrapped.bind(session)() if session is not None else wrapped()

        yield invoke


def test_globals_can_be_created_rebound_and_mutated_across_session_calls(stack_call: StackCall):
    with Session() as session:
        # Even the first call must not seed its namespace from client globals.
        with (
            pytest.warns(IgnoredGlobalsWarning),
            pytest.raises(RemoteExecutionError, match="NameError"),
        ):
            stack_call(
                """
                session_count = 99
                def task():
                    return session_count
                """,
                session,
            )
        assert (
            stack_call(
                """
            def task():
                global session_value, session_items, session_count
                session_value = 40
                session_items = ["loaded"]
                session_count = 0
        """,
                session,
            )
            is None
        )
        assert stack_call(
            """
            def task():
                return session_value, session_items
        """,
            session,
        ) == (40, ["loaded"])
        assert (
            stack_call(
                """
            def task():
                global session_value
                session_value += 2
                session_items.append("processed")
                return session_value
        """,
                session,
            )
            == 42
        )
        assert stack_call(
            """
            def task():
                return session_value, session_items
        """,
            session,
        ) == (42, ["loaded", "processed"])
        # Repeatedly send the original client initialization. The persisted
        # remote binding must win, rather than reset the count on every call.
        increment = """
            session_count = 0
            def task():
                global session_count
                session_count += 1
                return session_count
        """
        for expected in (1, 2, 3):
            with pytest.warns(IgnoredGlobalsWarning):
                assert stack_call(increment, session) == expected
        with pytest.raises(RemoteExecutionError, match="ValueError"):
            stack_call(
                """
                def task():
                    global session_count
                    session_count += 10
                    raise ValueError("after assignment")
                """,
                session,
            )
        assert (
            stack_call(
                """
            def task():
                return session_count
            """,
                session,
            )
            == 13
        )


def test_globals_are_isolated_between_sessions_and_unscoped_calls(stack_call: StackCall):
    writer = """
        def task():
            global session_value
            session_value = 42
    """
    presence = """
        def task():
            return "session_value" in globals()
    """
    with Session() as first, Session() as second:
        stack_call(writer, first)
        assert stack_call(presence, second) is False
        stack_call(
            """
            def task():
                global session_value
                session_value = 99
        """,
            second,
        )
        reader = """
            def task():
                return session_value
        """
        assert stack_call(reader, first) == 42
        assert stack_call(reader, second) == 99
        assert stack_call(presence, None) is False
        stack_call(writer, None)
        assert stack_call(presence, None) is False
        # An independent call must not clear an existing session's state either.
        assert stack_call(reader, first) == 42
