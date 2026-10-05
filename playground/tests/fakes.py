"""Fake backend: records what it was given and never runs anything."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from readie import RemoteExecutionError


class FakeBackend:
    def __init__(self, *, fail_cold: bool = False, hang: bool = False) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.cancelled = 0
        self._fail_cold = fail_cold
        self._hang = hang

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
