"""The error hierarchy.

Every failure a caller can see is one of these, so ``except ReadieError`` is
exhaustive. The previous client raised whatever gRPC or cloudpickle happened to
raise -- most memorably a bare ``EOFError`` from unpickling an empty buffer,
which is the *normal* outcome when a remote function raises.
"""

from __future__ import annotations


class ReadieError(Exception):
    """Base class for everything this package raises."""


class ConfigurationError(ReadieError):
    """The client was configured with something unusable.

    Raised at construction rather than at call time.
    """


class SerializationError(ReadieError):
    """The call could not be encoded, or the result could not be decoded.

    Encoding fails for functions that close over unpicklable state (an open
    socket, a thread, a local lambda in some interpreters). Decoding fails when
    the payload was produced by an incompatible interpreter or library version.
    """


class ClientClosedError(ReadieError):
    """The client, or the session, was used after being closed."""


class BlockingCallInEventLoopError(ReadieError):
    """A blocking call was made from inside a running event loop.

    Blocking there would stall the loop the response has to arrive on, so this
    is reported instead of deadlocking. Use ``await fn.aio(...)``.
    """


# ---------------------------------------------------------------------------
# Transport: the call never reached a healthy executor.
# ---------------------------------------------------------------------------
class TransportError(ReadieError):
    """The router could not be reached, or refused the call."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        """The gRPC status code name, when the failure came from the wire."""


class ClusterUnavailableError(TransportError):
    """No router, or no worker with capacity.

    A cold router with no registered workers answers UNAVAILABLE. So does a
    router that is down. They are the same problem from here: retry later.
    """


class RemoteTimeoutError(TransportError):
    """The call exceeded its deadline.

    Named to avoid shadowing the builtin ``TimeoutError``, which callers may
    reasonably want to catch separately around their own code.
    """


class InvalidRequestError(TransportError):
    """The router rejected the request as malformed."""


class ResourceExhaustedError(TransportError):
    """The cluster is at capacity, or the payload exceeded a message limit."""


class ExecutionCancelledError(TransportError):
    """The call was cancelled, locally or by the router."""


class PermissionDeniedError(TransportError):
    """The router refused the call on authorization grounds."""


# ---------------------------------------------------------------------------
# Execution: the call ran, and did not produce a usable result.
# ---------------------------------------------------------------------------
class ExecutionError(ReadieError):
    """The remote function ran but produced no usable result."""

    def __init__(
        self,
        message: str,
        *,
        logs: tuple[str, ...] = (),
        worker_id: str = "",
        container_id: str = "",
    ) -> None:
        super().__init__(self._render(message, logs))
        self.logs = logs
        """Everything the executor wrote to stdout and stderr, in order."""
        self.worker_id = worker_id
        """The worker that ran the call, when the router reported one."""
        self.container_id = container_id
        """The container that ran the call, when the router reported one."""

    @staticmethod
    def _render(message: str, logs: tuple[str, ...]) -> str:
        if not logs:
            return message
        # The remote traceback is in here and nowhere else, so inline it rather
        # than leaving it on an attribute nobody thinks to look at.
        tail = "\n".join(line.rstrip() for line in logs[-40:])
        return f"{message}\n--- remote output ---\n{tail}"


class RemoteExecutionError(ExecutionError):
    """The remote function raised.

    ``remote_traceback`` is the traceback from the process that actually ran the
    call, formatted there and sent back in the result envelope. It is the real
    one -- the frames below are this client's own and say nothing useful.

    The exception object itself is deliberately not reconstructed: unpickling it
    would need its class importable *here*, which for a library that exists only
    in the worker image it is not, and the resulting ImportError would replace
    the real error with a confusing one.
    """

    def __init__(
        self,
        message: str,
        *,
        logs: tuple[str, ...] = (),
        worker_id: str = "",
        container_id: str = "",
        remote_type: str = "",
        remote_message: str = "",
        remote_traceback: str = "",
    ) -> None:
        if remote_traceback:
            # The remote traceback is the only one that explains anything; the
            # local frames are all transport. Inlined so it appears in an
            # unhandled-exception dump rather than only on an attribute.
            message = f"{message}\n--- remote traceback ---\n{remote_traceback.rstrip()}"
        super().__init__(message, logs=logs, worker_id=worker_id, container_id=container_id)
        self.remote_type = remote_type
        """Class name of the exception the remote function raised."""
        self.remote_message = remote_message
        """``str()`` of that exception."""
        self.remote_traceback = remote_traceback
        """Its formatted traceback, from the process that raised it."""


class EmptyResultError(ExecutionError):
    """The worker returned no payload at all.

    This is now a protocol violation rather than an everyday outcome. Under
    executor protocol 1 it was how *every* remote exception surfaced: the
    executor caught it, printed the traceback to stderr, and wrote nothing, so
    the worker saw a clean stream and reported success. Protocol 2 sends a
    result envelope for both outcomes, so a raising function raises
    ``RemoteExecutionError`` with its real traceback and reaching here means the
    executor died without saying anything.

    A function returning ``None`` does not land here either: that is a value,
    and it travels in the envelope.
    """
