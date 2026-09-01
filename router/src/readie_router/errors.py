"""Domain exceptions.

These are raised by the scheduling layer, which knows nothing about gRPC, and
translated into status codes at the transport boundary by
``grpcserver.status``. Keeping them distinct is what lets a caller tell "no
workers have registered yet" from "every worker is full" — two conditions the
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


class InvalidRequestError(RouterError):
    """The caller sent something the protocol does not allow."""


class WorkerUnavailableError(RouterError):
    """The chosen worker could not be reached."""

    def __init__(self, worker_id: str, worker_uri: str, cause: BaseException | None = None) -> None:
        super().__init__(f"worker {worker_id} at {worker_uri} is unreachable")
        self.worker_id = worker_id
        self.worker_uri = worker_uri
        self.__cause__ = cause
