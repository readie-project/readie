"""The cluster's data model.

Dataclasses, not dicts. The previous implementation built these records as dict
literals, and a single missing comma silently produced a key called
``checkpoint_idcpu_util`` while leaving both ``checkpoint_id`` and ``cpu_util``
absent - a class of bug that cannot occur here.

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

# Resource kinds, mirroring ResourceKind in protos/resources.proto. Redeclared
# rather than imported so the domain does not depend on generated code; a test
# asserts the mapping.
RESOURCE_MEMORY = 1
RESOURCE_GPU_MEMORY = 2


class Outcome(enum.Enum):
    """How an execution finished, as far as placement is concerned."""

    SUCCESS = enum.auto()
    FAILURE = enum.auto()


@dataclass(frozen=True, slots=True)
class Budget:
    """One resource budget in bytes: the initial reservation and its ceiling.

    ``kind`` is a ``RESOURCE_*`` value. ``max`` of 0 means the worker may expand
    the budget up to its own capacity.
    """

    kind: int
    alloc: int
    max: int = 0


@dataclass(frozen=True, slots=True)
class Demand:
    """What a request needs: a budget per resource kind, plus import hints.

    Memory is a byte count that the worker maps onto a container memory limit;
    GPU memory is device memory. The import hints (``resources``) are forwarded
    to the worker for checkpoint selection, not used for placement.
    """

    budgets: tuple[Budget, ...] = ()
    resources: tuple[str, ...] = ()
    #: "cpu" or "gpu". A gpu demand may run only on gpu workers; a cpu demand
    #: prefers cpu workers but may spill to gpu workers when none are free.
    flavor: str = "cpu"
    #: When true, skip checkpoint selection for this request and cold-start.
    #: Session affinity still applies; the scheduler rejects this only when the
    #: session's warm container was itself restored from a checkpoint.
    disable_optimized_execution: bool = False

    def alloc_of(self, kind: int) -> int:
        """Return the initial byte budget for ``kind``, or 0 if none is set."""
        for budget in self.budgets:
            if budget.kind == kind:
                return budget.alloc
        return 0


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
    #: "cpu" or "gpu". Selects which generation's catalogue a checkpoint is
    #: chosen from, and gates flavor-aware placement. Advertised by the worker.
    flavor: str = "cpu"
    executors: dict[str, ExecutorRecord] = field(default_factory=dict)

    # Pushed by the worker.
    cpu_util: int = 0
    cpu_total: int = 0
    gpu_util: int = 0
    gpu_total: int = 0
    mem_used: int = 0
    mem_total: int = 0
    #: GPU device-memory capacity, so GPU requests place on headroom the way
    #: system memory does. Zero on a cpu worker.
    gpu_mem_used: int = 0
    gpu_mem_total: int = 0
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
    """A client session and the container it is pinned to.

    ``expired`` is a tombstone, not a deletion: once the container backing a
    session is confirmed gone, the record stays - kept forever, since only
    genuine, opted-in sessions ever get one - so that a caller reusing the
    same session_id is told it has expired instead of silently cold-starting
    under an id that looks like it still carries warm state.
    """

    session_id: str
    affinity: Affinity | None = None
    requests: set[str] = field(default_factory=set)
    last_seen: float = 0.0
    expired: bool = False

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
    budgets: tuple[Budget, ...]
    resources: tuple[str, ...]
    warm: bool
    opened_at: float

    def alloc_of(self, kind: int) -> int:
        """Return the initial byte budget for ``kind``, or 0 if none is set."""
        for budget in self.budgets:
            if budget.kind == kind:
                return budget.alloc
        return 0


@dataclass(frozen=True, slots=True)
class WorkerView:
    """A read-only projection of a worker, handed to the selection policy.

    A projection rather than the record itself so a policy cannot mutate state
    while deciding.
    """

    worker_id: str
    worker_uri: str
    flavor: str
    mem_used: int
    mem_total: int
    reserved_bytes: int
    gpu_mem_used: int
    gpu_mem_total: int
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
            flavor=worker.flavor,
            mem_used=worker.mem_used,
            mem_total=worker.mem_total,
            reserved_bytes=worker.reserved_bytes,
            gpu_mem_used=worker.gpu_mem_used,
            gpu_mem_total=worker.gpu_mem_total,
            inflight=worker.inflight,
            executor_count=max(worker.executor_count, len(worker.executors)),
            max_executors=worker.max_executors,
            cpu_util=worker.cpu_util,
            cpu_total=worker.cpu_total,
        )

    def capacity(self, kind: int) -> tuple[int, int]:
        """Return ``(used, total)`` bytes for a resource kind.

        Memory folds in the reservation held for placements that have not
        reported back yet. GPU memory is reported as the worker last measured
        it. A kind with no capacity signal reports ``(0, 0)``, which makes the
        headroom filter inert.
        """
        if kind == RESOURCE_MEMORY:
            return (self.mem_used + self.reserved_bytes, self.mem_total)
        if kind == RESOURCE_GPU_MEMORY:
            return (self.gpu_mem_used, self.gpu_mem_total)
        return (0, 0)
