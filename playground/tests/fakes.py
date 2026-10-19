"""Fake backend: records what it was given and never runs anything."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from readie import RemoteExecutionError, RemoteTimeoutError

from readie_playground.runner import EvalOptions


@dataclass(frozen=True, slots=True)
class EvalCall:
    """One ``execute_eval`` call as the backend received it."""

    source: str
    key: str
    options: EvalOptions


class FakeBackend:
    def __init__(
        self,
        *,
        fail_cold: bool = False,
        hang: bool = False,
        fail_eval: bool = False,
        timeout_eval: bool = False,
        crash_eval: bool = False,
        output: str = "",
    ) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.evals: list[EvalCall] = []
        self.cancelled = 0
        self._fail_cold = fail_cold
        self._hang = hang
        self._fail_eval = fail_eval
        self._timeout_eval = timeout_eval
        self._crash_eval = crash_eval
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

    async def execute_eval(
        self,
        code: str,
        *,
        key: str,
        options: EvalOptions,
        on_log: Callable[[str], None],
    ) -> None:
        self.evals.append(EvalCall(code, key, options))
        try:
            if self._hang:
                await asyncio.sleep(3600)
            if self._output:
                on_log(self._output)
            if self._fail_eval:
                raise RemoteExecutionError("boom", remote_traceback="Traceback: ValueError")
            if self._timeout_eval:
                message = "deadline exceeded"
                raise RemoteTimeoutError(message)
            if self._crash_eval:
                message = "unexpected"
                raise RuntimeError(message)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
