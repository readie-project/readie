"""Session globals across the real codec and socket boundary, without routing or gVisor.

Namespace-only behavior belongs in test_globals.py. Fresh, unimportable client
namespaces force cloudpickle to serialize functions by value rather than give
remote code access to this test process's globals.
"""

from textwrap import dedent
from types import FunctionType

import pytest

from readie_executor.config import Settings
from readie_executor.server import ExecutorServer
from tests.integration.test_server import CHUNK, exchange, value_of
from tests.integration.test_server import call as make_call

CLIENT_CONSTANT = 99


def read_client_constant() -> int:
    return CLIENT_CONSTANT


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
    monkeypatch.setattr("readie_executor.server.install_packages", lambda _: "")
    return ExecutorServer(Settings(socket_dir="/unused", chunk_size=CHUNK))


def test_remote_rebindings_and_mutations_survive_reserializing_client_initializations(
    executor: ExecutorServer,
):
    writer = function("""
        def task():
            global session_value, session_count, session_items
            session_value = 40
            session_count = 0
            session_items = []
    """)
    reader = function("""
        def task():
            return session_value, session_count, tuple(session_items)
    """)
    update = function("""
        session_value = 99
        session_count = 0
        session_items = []
        def task(item, step=1):
            global session_value, session_count
            session_value += step
            session_count += 1
            session_items.append(item)
            return session_value, session_count, tuple(session_items)
    """)
    assert "session_value" not in reader.__globals__
    assert value_of(exchange(call(writer), executor=executor)) is None
    assert value_of(exchange(call(reader), executor=executor)) == (40, 0, ())
    assert value_of(exchange(call(update, "first"), executor=executor)) == (41, 1, ("first",))
    assert value_of(exchange(call(update, "second", step=2), executor=executor)) == (
        43,
        2,
        ("first", "second"),
    )
    assert value_of(exchange(call(reader), executor=executor)) == (43, 2, ("first", "second"))
    assert "session_value" not in writer.__globals__
    assert "session_value" not in reader.__globals__
    assert update.__globals__["session_value"] == 99
    assert update.__globals__["session_count"] == 0
    assert update.__globals__["session_items"] == []


def test_client_globals_are_discarded_even_on_the_first_call(executor: ExecutorServer):
    task = function("""
        client_value = 99
        def task():
            return client_value
    """)
    envelope = exchange(call(task), executor=executor)
    assert envelope["ok"] is False
    assert envelope["exc_type"] == "NameError"
    assert envelope["session_globals_applied"] is True
    assert envelope["warnings"]


def test_deleted_globals_are_not_resurrected_from_the_client(executor: ExecutorServer):
    invoke(
        executor,
        """
        def task():
            global client_value
            client_value = 42
            del client_value
    """,
    )
    task = function("""
        client_value = 99
        def task():
            return client_value
    """)
    envelope = exchange(call(task), executor=executor)
    assert envelope["ok"] is False
    assert envelope["exc_type"] == "NameError"
    assert "client_value" not in executor._session_globals


def test_executors_are_isolated_and_unscoped_calls_do_not_clear_session_state(
    executor: ExecutorServer,
):
    other = ExecutorServer(Settings(socket_dir="/unused", chunk_size=CHUNK))
    writer = """
        def task(value):
            global client_value
            client_value = value
    """
    invoke(executor, writer, 42)
    assert (
        invoke(
            other,
            """
        def task():
            return "client_value" in globals()
    """,
        )
        is False
    )
    invoke(other, writer, 7)
    task = function("""
        client_value = 99
        def task():
            return client_value
    """)
    envelope = exchange(make_call(task), executor=executor)
    assert value_of(envelope) == 99
    assert "warnings" not in envelope
    assert "session_globals_applied" not in envelope
    envelope = exchange(call(task), executor=executor)
    assert value_of(envelope) == 42
    assert envelope["session_globals_applied"] is True
    assert value_of(exchange(call(task), executor=other)) == 7
    assert task.__globals__["client_value"] == 99


def test_by_reference_functions_do_not_corrupt_their_original_module(executor: ExecutorServer):
    # cloudpickle resolves this importable function to this very module. Clearing
    # its dictionary would break the test runner as well as violate isolation.
    envelope = exchange(call(read_client_constant), executor=executor)
    assert envelope["exc_type"] == "NameError"
    assert read_client_constant.__globals__["CLIENT_CONSTANT"] == 99
    invoke(
        executor,
        """
        def task():
            global CLIENT_CONSTANT
            CLIENT_CONSTANT = 42
    """,
    )
    assert value_of(exchange(call(read_client_constant), executor=executor)) == 42
    assert read_client_constant() == 99


def test_an_unsupported_callable_does_not_destroy_the_session(executor: ExecutorServer):
    invoke(
        executor,
        """
        def task():
            global saved
            saved = 42
    """,
    )
    envelope = exchange(call(len, []), executor=executor)
    assert envelope["exc_type"] == "TypeError"
    assert "plain Python functions only" in str(envelope["message"])
    assert (
        invoke(
            executor,
            """
        def task():
            return saved
    """,
        )
        == 42
    )
    assert value_of(exchange(make_call(len, []), executor=executor)) == 0


def test_warnings_survive_installation_and_dns_failures(
    executor: ExecutorServer,
    monkeypatch: pytest.MonkeyPatch,
):
    def fail(*_: object) -> str:
        raise OSError("setup failed")

    task = function("""
        client_value = 99
        def task():
            return client_value
    """)
    for seam in ("install_packages", "ensure_dns"):
        with monkeypatch.context() as patch:
            patch.setattr(f"readie_executor.server.{seam}", fail)
            envelope = exchange(call(task), executor=executor)
        assert envelope["exc_type"] == "OSError"
        assert envelope["session_globals_applied"] is True
        assert envelope["warnings"]
        assert "client_value" not in executor._session_globals


def test_output_warnings_and_state_survive_result_serialization_failure(executor: ExecutorServer):
    task = function("""
        client_value = 99
        def task():
            global saved
            import threading
            print("before result")
            saved = 42
            if "client_value" in globals():
                raise AssertionError(client_value)
            return threading.Lock()
    """)
    envelope = exchange(call(task), executor=executor)
    assert envelope["ok"] is False
    assert envelope["session_globals_applied"] is True
    assert envelope["output"] == ["before result", "\n"]
    assert envelope["warnings"]
    assert (
        invoke(
            executor,
            """
        def task():
            return saved
    """,
        )
        == 42
    )


def test_captures_survive_the_round_trip_but_are_deserialized_fresh_for_each_call(
    executor: ExecutorServer,
):
    invoke(
        executor,
        """
        def task():
            global saved
            saved = 42
    """,
    )
    task = function("""
        def make():
            factor = 7
            def task(value=6, *, offset=0):
                return value * factor + offset, saved
            return task
        task = make()
    """)
    assert value_of(exchange(call(task), executor=executor)) == (42, 42)
    assert value_of(exchange(call(task, offset=1), executor=executor)) == (43, 42)
    mutable = function("""
        def make():
            captured = []
            def task(item):
                captured.append(item)
                return captured
            return task
        task = make()
    """)
    assert value_of(exchange(call(mutable, "first"), executor=executor)) == ["first"]
    assert value_of(exchange(call(mutable, "second"), executor=executor)) == ["second"]
    assert "captured" not in executor._session_globals


def test_a_failed_call_keeps_state_and_diagnostics_and_the_executor_remains_usable(
    executor: ExecutorServer,
):
    # Notebook-like state is not transactional: a user exception doesn't undo
    # assignments already executed, and the warm interpreter remains usable.
    failing = function("""
        client_value = 99
        def task():
            global saved
            saved = 42
            # cloudpickle only includes referenced client globals.
            if "client_value" in globals():
                raise AssertionError(client_value)
            raise ValueError("after assignment")
    """)
    envelope = exchange(call(failing), executor=executor)
    assert envelope["ok"] is False
    assert envelope["exc_type"] == "ValueError"
    assert envelope["message"] == "after assignment"
    assert envelope["session_globals_applied"] is True
    assert envelope["warnings"]
    assert (
        invoke(
            executor,
            """
        def task():
            return saved
    """,
        )
        == 42
    )
