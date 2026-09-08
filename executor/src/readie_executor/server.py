"""The socket server: accept, run one call, reply, repeat."""

from __future__ import annotations

import contextlib
import io
import socket
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

import cloudpickle

from readie_executor import protocol
from readie_executor.codec import Call, DecodeError, decode_call, encode_result
from readie_executor.config import Settings
from readie_executor.install import install_packages, installed_versions

if TYPE_CHECKING:
    from typing import Any

#: The kernel's sun_path limit: 108 bytes on Linux, 104 on macOS. Checking
#: against the smaller of the two costs nothing and makes the failure the same
#: everywhere. The worker builds this path as
#: <workerDir>/<containerID>/executor.sock, so a deep worker directory hits it,
#: and the bare EADDRINUSE-adjacent errno reads as "the executor never started".
MAX_SOCKET_PATH_BYTES = 104

#: One connection at a time, by design. A container is a single interpreter
#: behind a single socket and cannot serve two executions concurrently; the
#: router knows this and serialises a session's calls. A deeper backlog here
#: would accept work this process cannot start.
LISTEN_BACKLOG = 1


class _OutputTee(io.TextIOBase):
    """Write function output to the sandbox log and retain it for the client."""

    def __init__(self, target: TextIO, output: list[str]) -> None:
        self._target = target
        self._output = output

    def write(self, text: str) -> int:
        written = self._target.write(text)
        self._target.flush()
        self._output.append(text)
        return written

    def flush(self) -> None:
        self._target.flush()


class ExecutorServer:
    """Serves execution requests over a unix socket.

    The executor is the *server* and the worker dials it. That is the reverse of
    what the direction of work suggests and is why the worker needs a dial
    budget at all.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._socket: socket.socket | None = None
        self._closed = False

    @property
    def path(self) -> str:
        """The socket path being served."""
        return self._settings.socket_path

    def bind(self) -> None:
        """Create and bind the socket, replacing any stale one."""
        path = Path(self._settings.socket_path)

        encoded = len(str(path).encode())
        if encoded >= MAX_SOCKET_PATH_BYTES:
            msg = (
                f"socket path is {encoded} bytes, over the {MAX_SOCKET_PATH_BYTES}-byte "
                f"kernel limit for unix sockets: {path}. Shorten EXECUTOR_DIR."
            )
            raise OSError(msg)

        # A crashed predecessor leaves the inode behind, and bind() fails with
        # EADDRINUSE on a socket nothing is listening to.
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(path))
            server.listen(LISTEN_BACKLOG)
        except OSError:
            server.close()
            raise

        # The worker runs as a different uid to whatever the sandbox chose, and
        # a socket it cannot open is indistinguishable from an executor that
        # never started.
        with contextlib.suppress(OSError):
            path.chmod(0o666)

        self._socket = server
        print(f"[executor] listening on {path}", flush=True)

    def serve_forever(self) -> None:
        """Handle connections until the listener is closed."""
        if self._socket is None:
            msg = "bind() must be called before serve_forever()"
            raise RuntimeError(msg)

        # Bound to a local. close() may run on another thread and sets the
        # attribute to None, so re-reading it each iteration would dereference
        # None rather than observing a closed socket.
        listener = self._socket

        while True:
            try:
                conn, _ = listener.accept()
                print("[executor] listening for request", flush=True)
            except (KeyboardInterrupt, SystemExit):
                raise
            except OSError as exc:
                if self._closed:
                    # An ordinary shutdown: close() unblocked this accept.
                    return
                # The listener itself failed. Continuing would spin.
                print(f"[executor] accept failed: {exc}", file=sys.stderr, flush=True)
                return

            with conn:
                self.handle(conn)

    def handle(self, conn: socket.socket) -> None:
        """Serve one request on an accepted connection.

        Every failure mode is contained here: the executor must survive a bad
        request and stay available for the next one, because the container it
        lives in is meant to be reused.
        """
        try:
            print("[executor] received request", flush=True)
            raw = protocol.read_message(conn, chunk_size=self._settings.chunk_size)
        except protocol.ProtocolError as exc:
            # A framing failure means the peer is not speaking this protocol, so
            # there is nothing it would understand coming back. Log and close.
            print(f"[executor] malformed request: {exc}", file=sys.stderr, flush=True)
            return

        envelope, output = self._run(raw)
        envelope["output"] = output

        try:
            payload = encode_result(envelope)
        except Exception as exc:  # noqa: BLE001 - cloudpickle raises broadly
            # The value came back but will not travel. Report *that* rather than
            # closing silently, which would look identical to a crash.
            payload = encode_result(
                protocol.failure_envelope(exc, traceback.format_exc()),
            )

        try:
            protocol.write_message(conn, payload, chunk_size=self._settings.chunk_size)
        except OSError as exc:
            # The worker hung up mid-response, usually because the client
            # cancelled. Not this process's problem, and not worth a traceback.
            print(f"[executor] could not send response: {exc}", file=sys.stderr, flush=True)

    def _run(self, raw: bytes) -> tuple[dict[str, Any], list[str]]:
        """Decode, install packages, and invoke, turning any failure into a response.

        Every path returns an envelope. Version 1 returned nothing when the
        function raised, so the caller saw an empty response and had to infer
        what happened from log lines; that inference is what this removes.

        Installing runs unconditionally, even for a package the rootfs already
        carries: every request restores a fresh container, so there is no warm
        state to check an "already installed" claim against.
        """
        try:
            call = decode_call(raw)
        except DecodeError as exc:
            print(f"[executor] {exc}", file=sys.stderr, flush=True)
            return protocol.failure_envelope(exc, traceback.format_exc()), []

        output: list[str] = []
        before: dict[str, str] = {}
        if call.packages:
            print(f"[executor] installing packages: {sorted(call.packages)}", flush=True)
            before = installed_versions()
        try:
            install_output = install_packages(list(call.packages))
        except Exception as exc:  # noqa: BLE001 - any install failure must produce a response, not a crash
            print(f"[executor] {exc}", file=sys.stderr, flush=True)
            return protocol.failure_envelope(exc, traceback.format_exc()), output
        if install_output:
            output.append(install_output)
            print("[executor] packages installed", flush=True)

        if call.packages and installed_versions() != before:
            # Something actually moved on disk -- possibly a native extension
            # at a different version than whatever a checkpoint restore left
            # resident in this process (see install.py). Invoking here would
            # risk two ABI-incompatible copies of the same extension in one
            # address space, which can crash the interpreter outright rather
            # than raise. A freshly spawned interpreter has nothing preloaded,
            # so it always reads what is on disk right now. A pin already
            # satisfied changes nothing, so that case stays on the fast,
            # already-warm path below.
            return self._invoke_in_subprocess(raw, output)

        return self._invoke_in_process(call, output)

    def _invoke_in_process(self, call: Call, output: list[str]) -> tuple[dict[str, Any], list[str]]:
        """Run ``call`` directly in this process and turn any failure into a response."""
        try:
            with (
                contextlib.redirect_stdout(_OutputTee(sys.stdout, output)),
                contextlib.redirect_stderr(_OutputTee(sys.stderr, output)),
            ):
                try:
                    value = call.invoke()
                except BaseException as exc:  # noqa: BLE001 - user code may raise anything
                    # Including SystemExit and KeyboardInterrupt: a called function
                    # raising either is still a failed call, not a reason to take the
                    # executor down and strand a container the worker means to reuse.
                    text = traceback.format_exc()
                    print(text, file=sys.stderr, flush=True)
                    return protocol.failure_envelope(exc, text), output
        except Exception as exc:  # noqa: BLE001 - preserve an executor failure as a response
            return protocol.failure_envelope(exc, traceback.format_exc()), output

        return protocol.success_envelope(value), output

    def _invoke_in_subprocess(
        self, raw: bytes, output: list[str]
    ) -> tuple[dict[str, Any], list[str]]:
        """Re-decode and invoke ``raw`` in a freshly spawned interpreter.

        ``readie_executor._invoke_subprocess`` does the decoding and invoking on
        the other end, so decode runs twice -- once here just to read
        ``packages``, once there to get ``func``/``args``/``kwargs`` -- but the
        call itself never touches a module this process already had loaded.
        """
        try:
            with tempfile.NamedTemporaryFile(delete=False) as handle:
                result_path = handle.name

            try:
                completed = subprocess.run(  # noqa: S603 - argv is fixed; raw travels as stdin
                    [sys.executable, "-m", "readie_executor._invoke_subprocess", result_path],
                    input=raw,
                    capture_output=True,
                    check=False,
                )
                for stream in (completed.stdout, completed.stderr):
                    text = stream.decode("utf-8", errors="replace")
                    if text:
                        output.append(text)

                result_bytes = Path(result_path).read_bytes()
            finally:
                Path(result_path).unlink(missing_ok=True)

            if not result_bytes:
                msg = f"call subprocess exited {completed.returncode} without a result"
                raise RuntimeError(msg)  # noqa: TRY301 - caught immediately below

            envelope: dict[str, Any] = cloudpickle.loads(result_bytes)
        except Exception as exc:  # noqa: BLE001 - preserve a subprocess failure as a response
            return protocol.failure_envelope(exc, traceback.format_exc()), output

        return envelope, output

    def close(self) -> None:
        """Stop listening and remove the socket.

        Idempotent, and safe to call from another thread while serve_forever is
        blocked in accept.
        """
        self._closed = True
        server, self._socket = self._socket, None
        if server is not None:
            server.close()
        with contextlib.suppress(OSError):
            Path(self._settings.socket_path).unlink()
