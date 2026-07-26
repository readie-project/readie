"""The cluster's data model.

Dataclasses, not dicts. The previous implementation built these records as dict
literals, and a single missing comma silently produced a key called
``checkpoint_idcpu_util`` while leaving both ``checkpoint_id`` and ``cpu_util``
absent — a class of bug that cannot occur here.

Nothing in this module imports gRPC. The scheduling layer is a pure state
machine so it can be tested without an event loop.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

# Status values, mirroring protos/registry.proto. Redeclared rather than
# imported so the domain does not depend on generated code; the mapping is
# asserted by a test.
#
# Note the gap at 1: the wire enum has no value 1 and this must match it.
STATUS_UNKNOWN = 0
STATUS_READY = 2
STATUS_BUSY = 3
STATUS_ERROR = 4
STATUS_REMOVED = 5


class Outcome(enum.Enum):
    """How an execution finished, as far as placement is concerned."""

    SUCCESS = enum.auto()
    FAILURE = enum.auto()


@dataclass(frozen=True, slots=True)
class Demand:
    """What a request needs.

    ``cpu_alloc`` is a byte count despite the name: the worker maps it onto a
    container memory limit. The name is fixed by the wire protocol.
    """

    cpu_alloc: int
    gpu_alloc: int
    resources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Affinity:
    """A session's binding to the container holding its Python state.

    ``seq`` is the lease that established this binding. Reconciliation is
    monotonic in it, so a slow response from an older request can never clobber
    a newer placement.
    """

    worker_id: str
    container_id: str
    seq: int


@dataclass(slots=True)
class ExecutorRecord:
    """One container on one worker."""

    container_id: str
    worker_id: str
    status: int = STATUS_UNKNOWN
    checkpoint_id: str = ""
    session_id: str = ""
    cpu_util: int = 0
    cpu_total: int = 0
    gpu_util: int = 0
    gpu_total: int = 0
    last_seen: float = 0.0

    @property
    def is_reusable(self) -> bool:
        """Whether a session may be resumed into this container.

        A container in ERROR is not reusable even though it still exists: the
        next request would resume a broken interpreter instead of cold-starting
        a working one.
        """
        return self.status in (STATUS_READY, STATUS_BUSY)


@dataclass(slots=True)
class WorkerRecord:
    """One worker node and everything the router knows about it."""

    worker_id: str
    worker_uri: str = ""
    status: int = STATUS_UNKNOWN
    executors: dict[str, ExecutorRecord] = field(default_factory=dict)

    # Pushed by the worker.
    cpu_util: int = 0
    cpu_total: int = 0
    gpu_util: int = 0
    gpu_total: int = 0
    mem_used: int = 0
    mem_total: int = 0
    executor_count: int = 0
    max_executors: int = 0

    # Tracked by the router.
    inflight: int = 0
    #: Memory committed to placements that have not reported back yet. Without
    #: this, two requests arriving between utilisation samples both see an idle
    #: worker and both land on it.
    reserved_bytes: int = 0
    last_seen: float = 0.0
    probe_failures: int = 0

    @property
    def is_selectable(self) -> bool:
        """Whether new work may be placed here."""
        return self.status == STATUS_READY and bool(self.worker_uri)


@dataclass(slots=True)
class SessionRecord:
    """A client session and the container it is pinned to."""

    session_id: str
    affinity: Affinity | None = None
    requests: set[str] = field(default_factory=set)
    last_seen: float = 0.0

    @property
    def is_idle(self) -> bool:
        """Whether no request for this session is currently running."""
        return not self.requests


@dataclass(frozen=True, slots=True)
class Placement:
    """The decision to run one request on one worker.

    Returned by the scheduler and held open until the request finishes. It is
    both the answer and the reservation: capacity is committed the moment this
    is created, so the next placement sees the load.
    """

    lease_id: int
    request_id: str
    session_id: str
    worker_id: str
    worker_uri: str
    container_id: str
    checkpoint_id: str
    cpu_alloc: int
    gpu_alloc: int
    resources: tuple[str, ...]
    warm: bool
    opened_at: float


@dataclass(frozen=True, slots=True)
class WorkerView:
    """A read-only projection of a worker, handed to the selection policy.

    A projection rather than the record itself so a policy cannot mutate state
    while deciding.
    """

    worker_id: str
    worker_uri: str
    mem_used: int
    mem_total: int
    reserved_bytes: int
    inflight: int
    executor_count: int
    max_executors: int
    cpu_util: int
    cpu_total: int

    @classmethod
    def of(cls, worker: WorkerRecord) -> WorkerView:
        """Project a record."""
        return cls(
            worker_id=worker.worker_id,
            worker_uri=worker.worker_uri,
            mem_used=worker.mem_used,
            mem_total=worker.mem_total,
            reserved_bytes=worker.reserved_bytes,
            inflight=worker.inflight,
            executor_count=max(worker.executor_count, len(worker.executors)),
            max_executors=worker.max_executors,
            cpu_util=worker.cpu_util,
            cpu_total=worker.cpu_total,
        )
