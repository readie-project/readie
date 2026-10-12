"""Fake backend: records what it was given and never runs anything."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from readie import RemoteExecutionError, RemoteTimeoutError

from readie_playground.runner import EvalOptions


@dataclass(frozen=True, slots=True)
class StepCall:
    """One ``execute_step`` call as the backend received it."""

    source: str
    index: int
    key: str
    session_id: str | None
    cold: bool
    options: EvalOptions


class FakeBackend:
    def __init__(
        self,
        *,
        fail_cold: bool = False,
        hang: bool = False,
        fail_steps: frozenset[int] = frozenset(),
        timeout_steps: frozenset[int] = frozenset(),
        crash_steps: frozenset[int] = frozenset(),
        rendezvous: asyncio.Barrier | None = None,
        output: str = "",
    ) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.steps: list[StepCall] = []
        self.cancelled = 0
        self._fail_cold = fail_cold
        self._hang = hang
        self._fail_steps = fail_steps
        self._timeout_steps = timeout_steps
        self._crash_steps = crash_steps
        self._rendezvous = rendezvous
        self._output = output

    async def execute(self, code: str, *, cold: bool, on_log: Callable[[str], None]) -> None:
        self.calls.append((code, cold))
        try:
            if self._hang:
                await asyncio.sleep(3600)
            on_log(f"{'cold' if cold else 'fast'} output\n")
            if cold and self._fail_cold:
                raise RemoteExecutionError("boom", remote_traceback="Traceback: ValueError")
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    async def execute_step(
        self,
        source: str,
        *,
        index: int,
        key: str,
        session_id: str | None,
        cold: bool,
        options: EvalOptions,
        on_log: Callable[[str], None],
    ) -> None:
        self.steps.append(StepCall(source, index, key, session_id, cold, options))
        try:
            if self._rendezvous is not None and index == 0:
                # Both lanes must be inside their first step at once to get past this.
                await asyncio.wait_for(self._rendezvous.wait(), timeout=5)
            if self._hang:
                await asyncio.sleep(3600)
            if self._output:
                on_log(self._output)
            if index in self._fail_steps:
                raise RemoteExecutionError("boom", remote_traceback="Traceback: ValueError")
            if index in self._timeout_steps:
                message = "deadline exceeded"
                raise RemoteTimeoutError(message)
            if index in self._crash_steps:
                message = "unexpected"
                raise RuntimeError(message)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
