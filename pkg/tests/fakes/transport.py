"""In-memory transports, for testing the client without a server."""

from __future__ import annotations

from collections.abc import Callable

import cloudpickle

from crfs.budget import Budget
from crfs.protocol import Attribution, CallRef, Outcome

# What the client handed the transport: ref, payload, imports, budgets, timeout, gpu.
Recorded = tuple[CallRef, bytes, tuple[str, ...], tuple[Budget, ...], float | None, bool]


class RecordingTransport:
    """Records what it was asked to do and returns a canned result."""

    def __init__(self, result: object = None, *, logs: tuple[str, ...] = ()) -> None:
        self.result = result
        self.envelope: dict[str, object] | None = None
        """Set to override the envelope entirely, e.g. to return a failure."""
        self.logs = logs
        self.error: BaseException | None = None
        self.calls: list[Recorded] = []
        self.closed = False

    def _run(
        self,
        ref: CallRef,
        payload: bytes,
        imports: tuple[str, ...],
        budgets: tuple[Budget, ...],
        *,
        timeout: float | None,
        on_log: Callable[[str], None] | None,
        gpu: bool = False,
    ) -> Outcome:
        self.calls.append((ref, payload, imports, budgets, timeout, gpu))
        if self.error is not None:
            raise self.error
        for line in self.logs:
            if on_log is not None:
                on_log(line)
        return Outcome(
            # Wrapped: the executor sends a result envelope, and the client
            # unwraps one. A fake returning a bare value would let the client's
            # unwrap path rot untested.
            payload=cloudpickle.dumps(
                self.envelope if self.envelope is not None else {"ok": True, "value": self.result}
            ),
            logs=self.logs,
            attribution=Attribution(worker_id="worker-fake", container_id="ctr-fake"),
        )

    def execute(
        self,
        ref: CallRef,
        payload: bytes,
        imports: tuple[str, ...],
        budgets: tuple[Budget, ...],
        *,
        timeout: float | None,
        on_log: Callable[[str], None] | None,
        gpu: bool = False,
    ) -> Outcome:
        return self._run(ref, payload, imports, budgets, timeout=timeout, on_log=on_log, gpu=gpu)

    def close(self) -> None:
        self.closed = True

    @property
    def last(self) -> Recorded:
        return self.calls[-1]


class AsyncRecordingTransport(RecordingTransport):
    """The same, awaitable."""

    async def execute(  # type: ignore[override]
        self,
        ref: CallRef,
        payload: bytes,
        imports: tuple[str, ...],
        budgets: tuple[Budget, ...],
        *,
        timeout: float | None,
        on_log: Callable[[str], None] | None,
        gpu: bool = False,
    ) -> Outcome:
        return self._run(ref, payload, imports, budgets, timeout=timeout, on_log=on_log, gpu=gpu)

    async def aclose(self) -> None:
        self.closed = True
