"""The server over a real socket."""

from __future__ import annotations

import socket
import tempfile
import threading
from pathlib import Path

import cloudpickle
import pytest

from readie_executor import protocol
from readie_executor.config import Settings
from readie_executor.server import ExecutorServer

CHUNK = 4096


def add(a: int, b: int) -> int:
    return a + b


def call(func: object, *args: object, packages: list[str] | None = None, **kwargs: object) -> bytes:
    return bytes(
        cloudpickle.dumps(
            {"func": func, "args": args, "kwargs": kwargs, "packages": packages or []}
        )
    )


def exchange(request: bytes, *, settings: Settings | None = None) -> dict[str, object]:
    """Drive one handle() over a socketpair and return the decoded envelope.

    A socketpair rather than a mock: the framing bugs worth catching are about
    real recv boundaries, and a mock cannot produce them.
    """
    server_side, client_side = socket.socketpair()
    executor = ExecutorServer(settings or Settings(socket_dir="/unused", chunk_size=CHUNK))

    def serve() -> None:
        # serve_forever wraps each connection in `with conn`; handle() alone
        # does not close it, and the reader below needs the close to see EOF.
        with server_side:
            executor.handle(server_side)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()

    protocol.write_message(client_side, request, chunk_size=CHUNK)

    # A deadline rather than a blocking read: a framing regression should fail
    # this test in seconds, not hang the suite.
    client_side.settimeout(10)
    try:
        raw = protocol.read_message(client_side, chunk_size=CHUNK)
    except TimeoutError:  # pragma: no cover - only on a regression
        pytest.fail("the executor neither responded nor closed the connection")
    finally:
        client_side.close()

    worker.join(timeout=10)
    assert not worker.is_alive(), "handle() did not return"
    return protocol.validate_envelope(cloudpickle.loads(raw))


def value_of(envelope: dict[str, object]) -> object:
    assert envelope["ok"] is True, envelope
    return envelope["value"]


def test_a_call_round_trips():
    assert value_of(exchange(call(add, 2, 40))) == 42


def test_function_output_is_returned_with_the_result():
    def speaks() -> str:
        print("Inside remote function")
        return "done"

    envelope = exchange(call(speaks))

    assert value_of(envelope) == "done"
    assert envelope["output"] == ["Inside remote function", "\n"]


def test_a_large_argument_survives_the_socket():
    blob = bytes(range(256)) * 4096  # 1 MiB, several chunks

    def echo(payload: bytes) -> bytes:
        return payload

    assert value_of(exchange(call(echo, blob))) == blob


def test_a_body_whose_bytes_are_the_old_terminator_survives():
    # Under version 1 these bytes *were* the terminator and could not appear in
    # a body at all.
    def echo(payload: bytes) -> bytes:
        return payload

    assert value_of(exchange(call(echo, b"trailing-EOF"))) == b"trailing-EOF"
    assert value_of(exchange(call(echo, b"EOF"))) == b"EOF"


def test_a_raising_function_returns_a_failure_envelope_with_its_traceback():
    # Version 1's defining flaw: this sent nothing at all, and the caller had to
    # infer failure from an empty response and scrape the traceback out of logs.
    def explode() -> None:
        raise ValueError("boom")

    envelope = exchange(call(explode))

    assert envelope["ok"] is False
    assert envelope["exc_type"] == "ValueError"
    assert envelope["message"] == "boom"
    assert "ValueError: boom" in str(envelope["traceback"])
    assert "explode" in str(envelope["traceback"]), "the frame that raised is named"


def test_a_function_raising_systemexit_does_not_take_the_executor_down():
    # A called function raising SystemExit is still a failed call, not a reason
    # to strand a container the worker means to reuse.
    def quit_abruptly() -> None:
        raise SystemExit(3)

    envelope = exchange(call(quit_abruptly))
    assert envelope["ok"] is False
    assert envelope["exc_type"] == "SystemExit"


def test_a_malformed_request_returns_a_failure_envelope():
    envelope = exchange(b"not a pickle")

    assert envelope["ok"] is False
    assert "unpickle" in str(envelope["message"])


def test_an_unpicklable_result_is_reported_rather_than_swallowed():
    # The value came back but will not travel. Version 1 closed the connection,
    # which looked identical to a crash.
    def make_a_lock() -> object:
        return threading.Lock()

    envelope = exchange(call(make_a_lock))
    assert envelope["ok"] is False


def test_packages_are_installed_before_the_call_runs(monkeypatch: pytest.MonkeyPatch):
    installed: list[list[str]] = []

    def fake_install(specs: list[str], **_: object) -> str:
        installed.append(specs)
        return ""

    monkeypatch.setattr("readie_executor.server.install_packages", fake_install)

    envelope = exchange(call(add, 1, 2, packages=["numpy", "requests==2.31.0"]))

    assert value_of(envelope) == 3
    assert installed == [["numpy", "requests==2.31.0"]]


def test_an_empty_packages_list_still_reads_correctly_and_runs_the_call(
    monkeypatch: pytest.MonkeyPatch,
):
    installed: list[list[str]] = []

    def fake_install(specs: list[str], **_: object) -> str:
        installed.append(specs)
        return ""

    monkeypatch.setattr("readie_executor.server.install_packages", fake_install)

    envelope = exchange(call(add, 1, 2))

    assert value_of(envelope) == 3
    assert installed == [[]]


def test_a_failed_install_returns_a_failure_envelope_without_running_the_call(
    monkeypatch: pytest.MonkeyPatch,
):
    from readie_executor.install import InstallError

    called = []

    def boom(specs, **_):
        raise InstallError("uv pip install failed (exit 1): no matching distribution")

    monkeypatch.setattr("readie_executor.server.install_packages", boom)

    def marks_that_it_ran() -> None:
        called.append(True)

    envelope = exchange(call(marks_that_it_ran, packages=["does-not-exist"]))

    assert envelope["ok"] is False
    assert "no matching distribution" in str(envelope["message"])
    assert called == []


def test_an_unexpected_install_failure_still_returns_a_failure_envelope(
    monkeypatch: pytest.MonkeyPatch,
):
    # install_packages shells out to a subprocess; anything other than a clean
    # non-zero exit (InstallError) - e.g. the binary itself being unusable -
    # must still produce a response, not leave the connection hanging with
    # nothing written to it.
    called = []

    def boom(specs, **_):
        raise FileNotFoundError("uv")

    monkeypatch.setattr("readie_executor.server.install_packages", boom)

    def marks_that_it_ran() -> None:
        called.append(True)

    envelope = exchange(call(marks_that_it_ran, packages=["numpy"]))

    assert envelope["ok"] is False
    assert "uv" in str(envelope["message"])
    assert called == []


def test_none_is_a_value_not_an_absence():
    # The whole reason for an envelope: "returned None" and "sent nothing" were
    # the same thing on the wire under version 1.
    def nothing() -> None:
        return None

    envelope = exchange(call(nothing))
    assert envelope["ok"] is True
    assert envelope["value"] is None


@pytest.fixture
def sockdir():
    """A short temporary directory.

    pytest's tmp_path lands under /private/var/folders/... on macOS, which alone
    is most of the 104-byte sun_path budget.
    """
    with tempfile.TemporaryDirectory(prefix="readie", dir="/tmp") as d:
        yield Path(d)


def test_a_socket_path_over_the_kernel_limit_says_so(tmp_path: Path):
    # The bare errno reads as "the executor never started", which sends you
    # looking in entirely the wrong place.
    deep = tmp_path / ("d" * 120)
    server = ExecutorServer(Settings(socket_dir=str(deep)))

    with pytest.raises(OSError, match="kernel limit"):
        server.bind()


def test_the_server_logs_its_bind_and_removes_its_socket(sockdir: Path, capsys):
    settings = Settings(socket_dir=str(sockdir))
    server = ExecutorServer(settings)

    server.bind()
    assert Path(settings.socket_path).exists()
    assert "[executor] listening on" in capsys.readouterr().out

    server.close()
    assert not Path(settings.socket_path).exists()


def test_binding_replaces_a_stale_socket_from_a_crashed_predecessor(sockdir: Path):
    settings = Settings(socket_dir=str(sockdir))
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(settings.socket_path)
    stale.close()  # Leaves the inode behind, as a crash would.

    server = ExecutorServer(settings)
    server.bind()  # Would fail EADDRINUSE without the unlink.
    server.close()


def test_close_is_idempotent(sockdir: Path):
    server = ExecutorServer(Settings(socket_dir=str(sockdir)))
    server.bind()
    server.close()
    server.close()


def test_serve_forever_before_bind_is_a_clear_error():
    server = ExecutorServer(Settings(socket_dir="/unused"))
    with pytest.raises(RuntimeError, match="bind"):
        server.serve_forever()


def test_a_bound_server_serves_two_requests_in_sequence(sockdir: Path):
    # A container is reused across executions, so the accept loop has to survive
    # one request finishing.
    settings = Settings(socket_dir=str(sockdir), chunk_size=CHUNK)
    server = ExecutorServer(settings)
    server.bind()

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        for a, b in ((1, 2), (10, 20)):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(settings.socket_path)
            protocol.write_message(client, call(add, a, b), chunk_size=CHUNK)

            raw = protocol.read_message(client, chunk_size=CHUNK)
            client.close()

            assert value_of(protocol.validate_envelope(cloudpickle.loads(raw))) == a + b
    finally:
        server.close()
        thread.join(timeout=5)
