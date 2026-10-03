"""Session payload flags, discard diagnostics, and failure compatibility."""

from __future__ import annotations

from functools import partial

import pytest

from readie_executor.config import Settings
from readie_executor.server import ExecutorServer
from tests.integration.test_server import CHUNK, call, exchange, value_of
from tests.integration.test_session_globals import function, invoke

CLIENT_CONSTANT = 99


def read_client_constant() -> int:
    return CLIENT_CONSTANT


@pytest.fixture
def executor(monkeypatch: pytest.MonkeyPatch) -> ExecutorServer:
    monkeypatch.setattr("readie_executor.server.ensure_dns", lambda: None)
    monkeypatch.setattr("readie_executor.server.install_packages", lambda _: "")
    return ExecutorServer(Settings(socket_dir="/unused", chunk_size=CHUNK))


def test_client_globals_are_discarded_even_on_the_first_call(executor: ExecutorServer):
    task = function("""
        client_value = 99
        def task():
            return client_value
    """)
    envelope = exchange(call(task, session_globals=True), executor=executor)
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
            return "client_value" in globals(), client_value
    """)
    envelope = exchange(call(task, session_globals=True), executor=executor)
    assert envelope["exc_type"] == "NameError"
    assert "client_value" not in executor._session_globals


def test_an_unscoped_call_uses_client_globals_without_clearing_session_state(
    executor: ExecutorServer,
):
    invoke(
        executor,
        """
        def task():
            global client_value
            client_value = 42
    """,
    )
    task = function("""
        client_value = 99
        def task():
            return client_value
    """)
    envelope = exchange(call(task), executor=executor)
    assert value_of(envelope) == 99
    assert "warnings" not in envelope
    assert "session_globals_applied" not in envelope
    envelope = exchange(call(task, session_globals=True), executor=executor)
    assert value_of(envelope) == 42
    assert envelope["session_globals_applied"] is True
    assert task.__globals__["client_value"] == 99


def test_by_reference_functions_do_not_corrupt_their_original_module(executor: ExecutorServer):
    # cloudpickle resolves this importable function to this very module. Clearing
    # its dictionary would break the test runner as well as violate isolation.
    envelope = exchange(call(read_client_constant, session_globals=True), executor=executor)
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
    assert (
        value_of(exchange(call(read_client_constant, session_globals=True), executor=executor))
        == 42
    )
    assert read_client_constant() == 99


@pytest.mark.parametrize("func", [len, partial(len, [])])
def test_unsupported_callables_do_not_destroy_the_session(executor: ExecutorServer, func):
    invoke(
        executor,
        """
        def task():
            global saved
            saved = 42
    """,
    )
    envelope = exchange(call(func, [], session_globals=True), executor=executor)
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
    assert value_of(exchange(call(len, []), executor=executor)) == 0


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
            envelope = exchange(call(task, session_globals=True), executor=executor)
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
            # Referencing a client global ensures it travels and is warned about,
            # even though the remote namespace, not its value, is what we inspect.
            if "client_value" in globals():
                raise AssertionError(client_value)
            return threading.Lock()
    """)
    envelope = exchange(call(task, session_globals=True), executor=executor)
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


def test_defaults_and_closures_survive_rebinding_after_unpickling(executor: ExecutorServer):
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
    assert value_of(exchange(call(task, session_globals=True), executor=executor)) == (42, 42)
    assert value_of(exchange(call(task, offset=1, session_globals=True), executor=executor)) == (
        43,
        42,
    )


def test_closure_objects_do_not_become_session_persistence(executor: ExecutorServer):
    task = function("""
        def make():
            captured = []
            def task(item):
                captured.append(item)
                return captured
            return task
        task = make()
    """)
    assert value_of(exchange(call(task, "first", session_globals=True), executor=executor)) == [
        "first"
    ]
    assert value_of(exchange(call(task, "second", session_globals=True), executor=executor)) == [
        "second"
    ]
    assert "captured" not in executor._session_globals


def test_functions_defined_and_published_remotely_inherit_the_session_namespace(
    executor: ExecutorServer,
):
    invoke(
        executor,
        """
        def task():
            global helper, saved
            saved = 42
            def helper():
                return saved
    """,
    )
    assert (
        invoke(
            executor,
            """
        def task():
            global saved
            saved += 1
            return helper()
    """,
        )
        == 43
    )


def test_function_name_is_not_automatically_registered(executor: ExecutorServer):
    assert (
        invoke(
            executor,
            """
        def task():
            return "task" in globals()
    """,
        )
        is False
    )
