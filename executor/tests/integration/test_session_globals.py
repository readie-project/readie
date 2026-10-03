"""Globals should persist across calls to the same session.

Represents each session with a different ExecutorServer. Test the real codec and
socket protocol, but not routing or gVisor. Functions are defined in an
unimportable module so cloudpickle cannot resolve them by reference, which would
give them access to the test process's own module globals.
"""

from textwrap import dedent
from types import FunctionType

import pytest

from readie_executor.config import Settings
from readie_executor.server import ExecutorServer
from tests.integration.test_server import CHUNK, exchange, value_of
from tests.integration.test_server import call as make_call


def call(func: object, *args: object, packages: list[str] | None = None, **kwargs: object) -> bytes:
    return make_call(func, *args, packages=packages, session_globals=True, **kwargs)


def function(source: str) -> FunctionType:
    namespace: dict[str, object] = {"__name__": "__readie_session_globals_test__"}
    exec(dedent(source), namespace)  # noqa: S102 - trusted, handwritten test functions
    func = namespace["task"]
    assert isinstance(func, FunctionType)
    return func


def invoke(executor: ExecutorServer, source: str, *args: object) -> object:
    return value_of(exchange(call(function(source), *args), executor=executor))


@pytest.fixture
def executor(monkeypatch: pytest.MonkeyPatch) -> ExecutorServer:
    # These workloads need neither DNS nor installation; don't touch host config.
    monkeypatch.setattr("readie_executor.server.ensure_dns", lambda: None)
    return ExecutorServer(Settings(socket_dir="/unused", chunk_size=CHUNK))


def test_a_global_created_remotely_is_readable_by_a_later_function(executor: ExecutorServer):
    writer = function("""
        def task(value):
            global session_value
            session_value = value
    """)
    reader = function("""
        def task():
            return session_value
    """)

    # No return-value threading or shared client namespace can supply this value.
    assert "session_value" not in reader.__globals__
    assert value_of(exchange(call(writer, 42), executor=executor)) is None
    assert value_of(exchange(call(reader), executor=executor)) == 42
    assert "session_value" not in writer.__globals__
    assert "session_value" not in reader.__globals__


def test_repeated_calls_keep_remote_rebindings_instead_of_resetting_to_client_globals(
    executor: ExecutorServer,
):
    increment = function("""
        session_count = 0
        def task(step=1):
            global session_count
            session_count += step
            return session_count
    """)

    invoke(
        executor,
        """
        def task():
            global session_count
            session_count = 0
        """,
    )
    assert value_of(exchange(call(increment), executor=executor)) == 1
    assert value_of(exchange(call(increment, step=2), executor=executor)) == 3
    assert value_of(exchange(call(increment, 4), executor=executor)) == 7
    # Each request reserializes the unchanged client value, not the remote count.
    assert increment.__globals__["session_count"] == 0


def test_mutations_survive_reserializing_the_original_client_object(executor: ExecutorServer):
    append = function("""
        session_items = []
        def task(item):
            session_items.append(item)
            return tuple(session_items)
    """)

    invoke(
        executor,
        """
        def task():
            global session_items
            session_items = []
        """,
    )
    assert value_of(exchange(call(append, "first"), executor=executor)) == ("first",)
    assert value_of(exchange(call(append, "second"), executor=executor)) == ("first", "second")
    assert append.__globals__["session_items"] == []
    assert invoke(
        executor,
        """
        def task():
            return tuple(session_items)
    """,
    ) == ("first", "second")


def test_a_different_function_can_rebind_an_existing_global(executor: ExecutorServer):
    invoke(
        executor,
        """
        def task():
            global session_value
            session_value = 10
    """,
    )
    assert (
        invoke(
            executor,
            """
        def task():
            global session_value
            session_value *= 3
            return session_value
    """,
        )
        == 30
    )
    assert (
        invoke(
            executor,
            """
        def task():
            return session_value
    """,
        )
        == 30
    )


@pytest.mark.parametrize("value", [None, False, 0, "", []])
def test_falsy_globals_are_not_treated_as_missing(executor: ExecutorServer, value: object):
    invoke(
        executor,
        """
        def task(value):
            global session_value
            session_value = value
    """,
        value,
    )
    assert invoke(
        executor,
        """
        def task():
            return "session_value" in globals(), session_value
    """,
    ) == (True, value)


def test_an_import_bound_globally_can_be_used_by_a_later_function(executor: ExecutorServer):
    invoke(
        executor,
        """
        def task():
            global session_math
            import math as session_math
    """,
    )
    assert (
        invoke(
            executor,
            """
        def task():
            return session_math.isqrt(81)
    """,
        )
        == 9
    )


def test_deleting_a_global_removes_it_for_subsequent_calls(executor: ExecutorServer):
    invoke(
        executor,
        """
        def task():
            global session_value
            session_value = 42
    """,
    )
    invoke(
        executor,
        """
        def task():
            global session_value
            del session_value
    """,
    )
    assert (
        invoke(
            executor,
            """
        def task():
            return "session_value" in globals()
    """,
        )
        is False
    )


def test_function_locals_do_not_become_session_globals(executor: ExecutorServer):
    assert (
        invoke(
            executor,
            """
        def task():
            local_value = 42
            return local_value
    """,
        )
        == 42
    )
    assert (
        invoke(
            executor,
            """
        def task():
            return "local_value" in globals()
    """,
        )
        is False
    )


def test_separate_executors_do_not_share_globals(executor: ExecutorServer):
    other = ExecutorServer(Settings(socket_dir="/unused", chunk_size=CHUNK))
    invoke(
        executor,
        """
        def task():
            global session_value
            session_value = "first container"
    """,
    )
    assert (
        invoke(
            other,
            """
        def task():
            return "session_value" in globals()
    """,
        )
        is False
    )
    invoke(
        other,
        """
        def task():
            global session_value
            session_value = "second container"
    """,
    )
    reader = """
        def task():
            return session_value
    """
    assert invoke(executor, reader) == "first container"
    assert invoke(other, reader) == "second container"


def test_a_failed_call_keeps_global_changes_made_before_the_exception(executor: ExecutorServer):
    # Notebook-like state is not transactional: a user exception doesn't undo
    # assignments already executed, and the warm interpreter remains usable.
    failing = function("""
        def task():
            global session_value
            session_value = 42
            raise ValueError("after assignment")
    """)
    envelope = exchange(call(failing), executor=executor)
    assert envelope["ok"] is False
    assert envelope["exc_type"] == "ValueError"
    assert envelope["message"] == "after assignment"
    assert (
        invoke(
            executor,
            """
        def task():
            return session_value
    """,
        )
        == 42
    )
