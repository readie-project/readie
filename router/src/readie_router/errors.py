"""Domain exceptions.

These are raised by the scheduling layer, which knows nothing about gRPC, and
translated into status codes at the transport boundary by
``grpcserver.status``. Keeping them distinct is what lets a caller tell "no
workers have registered yet" from "every worker is full" - two conditions the
previous router collapsed into an unhandled ``KeyError``.
"""

from __future__ import annotations


class RouterError(Exception):
    """Base class for every error this router raises deliberately."""


class SchedulingError(RouterError):
    """A request could not be placed."""


class NoWorkersRegisteredError(SchedulingError):
    """No worker has registered, or none is currently selectable.

    Transient by nature: workers register on startup and re-register after a
    restart, so a caller should retry.
    """

    def __init__(self) -> None:
        super().__init__("no workers are registered and healthy")


class NoCapacityError(SchedulingError):
    """Workers exist, but none can accept the request's allocation."""

    def __init__(self, *, candidates: int) -> None:
        super().__init__(f"none of the {candidates} healthy workers can accept this request")
        self.candidates = candidates


class SessionBusyError(SchedulingError):
    """Another request for this session is still running.

    A session maps to one container holding one Python process, so its requests
    are serialised. This is raised when the wait for a turn times out.
    """

    def __init__(self, session_id: str, *, waited: float) -> None:
        super().__init__(f"session {session_id} is still busy after {waited:g}s")
        self.session_id = session_id


class SessionExpiredError(SchedulingError):
    """The session's container has been torn down and the id was retired.

    Raised for a *known* session that has since expired - never for an id
    that has simply never been seen, which cold-starts normally instead. The
    id is not reusable: a caller must open a new session rather than retry
    this one, since reusing it would otherwise look like a warm resume that
    silently lost all of its state.
    """

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session {session_id} has expired")
        self.session_id = session_id


class OptimizedExecutionConflictError(SchedulingError):
    """A request asked to disable optimized execution for an already-optimized session.

    Session affinity always reuses the existing warm container regardless of
    how it started; silently ignoring the checkpoint origin, or tearing down
    an already-optimized session, would surprise the caller either way, so
    this is rejected instead.
    """

    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"session {session_id} is already running on a checkpoint-restored container"
        )
        self.session_id = session_id


class InvalidRequestError(RouterError):
    """The caller sent something the protocol does not allow."""


class WorkerUnavailableError(RouterError):
    """The chosen worker could not be reached."""

    def __init__(self, worker_id: str, worker_uri: str, cause: BaseException | None = None) -> None:
        super().__init__(f"worker {worker_id} at {worker_uri} is unreachable")
        self.worker_id = worker_id
        self.worker_uri = worker_uri
        self.__cause__ = cause
