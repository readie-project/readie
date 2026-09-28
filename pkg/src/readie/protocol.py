"""The wire protocol, as two pure objects.

Neither touches gRPC, a channel, a socket or an event loop -- they turn a call
into messages and messages back into a result. That is what lets every protocol
bug below be a unit test with no server running:

* the header must be sent first and exactly once, carrying the config
  (import hints and resource budgets);
* every message must repeat ``request_id`` and ``session_id``, because the
  router leases on the first and binds affinity on the second;
* ``success`` must be honoured rather than ignored;
* the payload is a *result envelope*, so a remote exception arrives as data --
  with its real traceback -- rather than as an absence the caller has to guess
  about.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, cast

from readie._proto import proxy_pb2, resources_pb2
from readie.budget import Budget
from readie.errors import EmptyResultError, RemoteExecutionError


@dataclass(frozen=True, slots=True)
class CallRef:
    """Identifies one execution to the router."""

    request_id: str
    session_id: str


def to_config(
    imports: tuple[str, ...],
    budgets: tuple[Budget, ...],
    gpu: bool = False,
    disable_optimized_execution: bool = False,
) -> proxy_pb2.ExecutionConfig:
    """Build the per-call config message: import hints, budgets and the GPU flag."""
    return proxy_pb2.ExecutionConfig(
        imports=list(imports),
        gpu=gpu,
        disable_optimized_execution=disable_optimized_execution,
        budgets=[
            resources_pb2.ResourceBudget(
                # Our IntEnum and the proto enum agree by value (a guard test
                # pins that); the cast is only for the type checker.
                kind=cast("resources_pb2.ResourceKind", b.kind.value),
                alloc=b.alloc,
                max=b.max,
            )
            for b in budgets
        ],
    )


class RequestEncoder:
    """Turns a payload into the request stream the router expects.

    The header is a separate message carrying the ``config`` arm of the oneof;
    payload messages carry the ``payload`` arm. Both arms cannot be set on one
    message, which is why the config cannot simply ride on the first chunk.
    """

    def __init__(self, *, chunk_size: int) -> None:
        if chunk_size <= 0:
            msg = f"chunk_size must be positive, got {chunk_size}"
            raise ValueError(msg)
        self._chunk_size = chunk_size

    def encode(
        self,
        ref: CallRef,
        payload: bytes,
        imports: tuple[str, ...] = (),
        budgets: tuple[Budget, ...] = (),
        *,
        gpu: bool = False,
        disable_optimized_execution: bool = False,
    ) -> Iterator[proxy_pb2.ClientExecutionRequest]:
        """Yield the header followed by the payload in chunks.

        A zero-byte payload still yields the header, so the router sees a
        well-formed -- if pointless -- request rather than an empty stream it
        would have to guess about.
        """
        yield proxy_pb2.ClientExecutionRequest(
            request_id=ref.request_id,
            session_id=ref.session_id,
            config=to_config(imports, budgets, gpu, disable_optimized_execution),
        )

        buffer = io.BytesIO(payload)
        try:
            while True:
                piece = buffer.read(self._chunk_size)
                if not piece:
                    break
                yield proxy_pb2.ClientExecutionRequest(
                    request_id=ref.request_id,
                    session_id=ref.session_id,
                    payload=piece,
                )
        finally:
            buffer.close()


@dataclass(slots=True)
class Attribution:
    """Where the call actually ran.

    A client cannot ask for a particular worker or container, but knowing which
    one served a slow call is the difference between "the platform is slow" and
    "one worker is slow".
    """

    worker_id: str = ""
    container_id: str = ""


class ResponseAssembler:
    """Accumulates a response stream into a result.

    Feed it every message, then call ``result``. It is deliberately not an
    iterator adapter: the sync and async transports differ in how they *get*
    messages and not at all in what they do with them, so this is the whole
    shared half.
    """

    def __init__(self, *, on_log: Callable[[str], None] | None = None) -> None:
        self._on_log = on_log
        self._payload = bytearray()
        self._logs: list[str] = []
        self._failed = False
        self._count = 0
        self.attribution = Attribution()

    @property
    def logs(self) -> tuple[str, ...]:
        """Executor output received so far, in arrival order."""
        return tuple(self._logs)

    @property
    def message_count(self) -> int:
        """How many messages arrived. Zero means the worker said nothing."""
        return self._count

    def accept(self, response: proxy_pb2.ClientExecutionResponse) -> None:
        """Fold one response message into the accumulating result."""
        self._count += 1

        # The worker stamps success=True on every message it sends; a False here
        # means something downstream marked the execution bad. Sticky, because a
        # later message must not erase it.
        if not response.success:
            self._failed = True

        # First non-empty wins: the router stamps these on every message, and a
        # later one cannot contradict the first without the call having moved.
        if not self.attribution.worker_id:
            self.attribution.worker_id = response.worker_id
        if not self.attribution.container_id:
            self.attribution.container_id = response.container_id

        arm = response.WhichOneof("data")
        if arm == "payload":
            self._payload.extend(response.payload)
        elif arm == "logs":
            line = response.logs
            self._logs.append(line)
            if self._on_log is not None:
                self._on_log(line)

    def result(self) -> bytes:
        """Return the collected envelope bytes, or raise with the captured output.

        An empty payload is a protocol violation, not an outcome. The executor
        sends a result envelope for success *and* failure, so nothing at all
        means it died without saying anything.
        """
        where = {
            "worker_id": self.attribution.worker_id,
            "container_id": self.attribution.container_id,
        }
        if self._failed:
            raise RemoteExecutionError(
                "the worker reported the execution as failed", logs=self.logs, **where
            )
        if not self._payload:
            raise EmptyResultError(
                "the worker returned no result at all, which means the executor "
                "died before it could report one",
                logs=self.logs,
                **where,
            )
        return bytes(self._payload)


@dataclass(slots=True)
class Outcome:
    """A completed call, before the payload is decoded."""

    payload: bytes
    logs: tuple[str, ...] = field(default_factory=tuple)
    attribution: Attribution = field(default_factory=Attribution)


def function_output(envelope: Any) -> tuple[str, ...]:
    """Extract stdout/stderr explicitly captured while the function ran."""
    if not isinstance(envelope, dict):
        return ()
    raw = envelope.get("output", ())
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return ()
    return tuple(raw)


def unwrap_envelope(envelope: Any, outcome: Outcome) -> Any:
    """Turn a decoded result envelope into a value, or raise.

    The envelope contract lives here beside the rest of the wire format rather
    than in the client. ``ok: False`` is not a transport or worker failure --
    the sandbox ran, the interpreter is healthy, the container is still warm --
    it is the user's own exception carried back as data.
    """
    output = function_output(envelope)
    outcome = Outcome(
        payload=outcome.payload,
        logs=outcome.logs + output,
        attribution=outcome.attribution,
    )
    where = {
        "worker_id": outcome.attribution.worker_id,
        "container_id": outcome.attribution.container_id,
    }

    if not isinstance(envelope, dict) or "ok" not in envelope:
        raise EmptyResultError(
            "the worker returned something that is not a result envelope; the "
            "executor may speak an older protocol version",
            logs=outcome.logs,
            **where,
        )

    if envelope["ok"]:
        return envelope.get("value")

    exc_type = str(envelope.get("exc_type", "Exception"))
    message = str(envelope.get("message", ""))
    raise RemoteExecutionError(
        f"the remote function raised {exc_type}: {message}",
        logs=outcome.logs,
        remote_type=exc_type,
        remote_message=message,
        remote_traceback=str(envelope.get("traceback", "")),
        **where,
    )
