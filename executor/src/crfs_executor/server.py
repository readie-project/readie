"""The socket server: accept, run one call, reply, repeat."""

from __future__ import annotations

import contextlib
import socket
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from crfs_executor import protocol
from crfs_executor.codec import DecodeError, decode_call, encode_result
from crfs_executor.config import Settings

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
            raw = protocol.read_message(conn, chunk_size=self._settings.chunk_size)
        except protocol.ProtocolError as exc:
            # A framing failure means the peer is not speaking this protocol, so
            # there is nothing it would understand coming back. Log and close.
            print(f"[executor] malformed request: {exc}", file=sys.stderr, flush=True)
            return

        envelope = self._run(raw)

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

    def _run(self, raw: bytes) -> dict[str, Any]:
        """Decode and invoke, turning any failure into a response.

        Every path returns an envelope. Version 1 returned nothing when the
        function raised, so the caller saw an empty response and had to infer
        what happened from log lines; that inference is what this removes.
        """
        try:
            call = decode_call(raw)
        except DecodeError as exc:
            print(f"[executor] {exc}", file=sys.stderr, flush=True)
            return protocol.failure_envelope(exc, traceback.format_exc())

        try:
            value = call.invoke()
        except BaseException as exc:  # noqa: BLE001 - user code may raise anything
            # Including SystemExit and KeyboardInterrupt: a called function
            # raising either is still a failed call, not a reason to take the
            # executor down and strand a container the worker means to reuse.
            text = traceback.format_exc()
            # Still printed, so it lands in the worker's captured logs where an
            # operator debugging the *worker* will look for it.
            print(text, file=sys.stderr, flush=True)
            return protocol.failure_envelope(exc, text)

        return protocol.success_envelope(value)

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
