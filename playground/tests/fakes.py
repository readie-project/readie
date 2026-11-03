"""Fake backend: records what it was given and never runs anything."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from readie import RemoteExecutionError, RemoteTimeoutError

from readie_playground.runner import ExecutionOptions


@dataclass(frozen=True, slots=True)
class ExecutionCall:
    """One ``execute`` call as the backend received it."""

    source: str
    key: str
    options: ExecutionOptions


class FakeBackend:
    def __init__(
        self,
        *,
        hang: bool = False,
        gate: asyncio.Event | None = None,
        fail_execution: bool = False,
        timeout_execution: bool = False,
        crash_execution: bool = False,
        output: str = "",
    ) -> None:
        self.calls: list[ExecutionCall] = []
        self.cancelled = 0
        self.started = asyncio.Event()
        self.call_started: asyncio.Queue[None] = asyncio.Queue()
        self._hang = hang
        self._gate = gate
        self._fail_execution = fail_execution
        self._timeout_execution = timeout_execution
        self._crash_execution = crash_execution
        self._output = output

    async def execute(
        self,
        code: str,
        *,
        key: str,
        options: ExecutionOptions,
        on_log: Callable[[str], None],
    ) -> None:
        self.calls.append(ExecutionCall(code, key, options))
        self.started.set()
        self.call_started.put_nowait(None)
        try:
            if self._gate is not None:
                await self._gate.wait()
            if self._hang:
                await asyncio.sleep(3600)
            if self._output:
                on_log(self._output)
            if self._fail_execution:
                raise RemoteExecutionError("boom", remote_traceback="Traceback: ValueError")
            if self._timeout_execution:
                message = "deadline exceeded"
                raise RemoteTimeoutError(message)
            if self._crash_execution:
                message = "unexpected"
                raise RuntimeError(message)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
