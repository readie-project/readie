"""Run one piece of visitor code on Readie twice, in parallel, and stream both.

The two lanes differ only in ``disable_optimized_execution``: the ``optimized``
lane uses checkpoint restore, the ``cold`` lane does not. Each lane builds the
visitor's script into a ``@remote`` function (``userfn``) and calls it through the
Readie client, as any user would.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from readie import Client, ReadieError, RemoteExecutionError, RemoteFunction, Settings
from readie.budget import build_budgets

from readie_playground import userfn
from readie_playground.config import MEMORY


class Lane(StrEnum):
    """Which start path a run used."""

    OPTIMIZED = "optimized"
    COLD = "cold"


@dataclass(frozen=True, slots=True)
class Event:
    """One frame of the response stream."""

    kind: str  # "log" or "done"
    lane: Lane
    text: str = ""
    ok: bool = True
    duration_s: float = 0.0
    error: str = ""
    truncated: bool = False


class Backend(Protocol):
    """Where a run actually happens. The production implementation calls Readie."""

    async def execute(self, code: str, *, cold: bool, on_log: Callable[[str], None]) -> None:
        """Run ``code`` remotely, calling ``on_log`` with output as it arrives."""
        ...


class ReadieBackend:
    """Calls Readie through the client, the way a user's script would."""

    def __init__(self, settings: Settings, timeout_s: float, workdir: Path) -> None:
        self._settings = settings
        self._timeout_s = timeout_s
        self._workdir = workdir
        # Fixed budget: equal initial and maximum, no visitor control, no session.
        self._budgets = build_budgets(memory=MEMORY, max_memory=MEMORY)

    async def execute(self, code: str, *, cold: bool, on_log: Callable[[str], None]) -> None:
        """Build the script into a function and call it. The log sink is per client."""
        with userfn.loaded(code, self._workdir) as main:
            async with Client(self._settings, log_sink=on_log) as client:
                remote_main = RemoteFunction(
                    main,
                    client=client,
                    budgets=self._budgets,
                    timeout=self._timeout_s,
                    disable_optimized_execution=cold,
                )
                await remote_main.aio()


class Runner:
    """Fans one request out to both lanes and merges their events."""

    def __init__(self, backend: Backend, max_output_chars: int) -> None:
        self._backend = backend
        self._max_output = max_output_chars

    async def stream(self, code: str) -> AsyncGenerator[Event, None]:
        """Yield log and done events from both lanes as they happen."""
        queue: asyncio.Queue[Event] = asyncio.Queue()
        error = userfn.check(code)
        if error is not None:
            # Not valid Python: report it on both lanes without calling Readie.
            for lane in Lane:
                yield Event("done", lane, ok=False, error=error)
            return
        tasks = [asyncio.create_task(self._lane(code, lane, queue)) for lane in Lane]
        try:
            remaining = len(tasks)
            while remaining:
                event = await queue.get()
                if event.kind == "done":
                    remaining -= 1
                yield event
        finally:
            # Client went away or the generator closed: stop paying for both runs.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _lane(self, code: str, lane: Lane, queue: asyncio.Queue[Event]) -> None:
        sent = 0
        truncated = False

        def on_log(text: str) -> None:
            nonlocal sent, truncated
            if truncated:
                return
            room = self._max_output - sent
            if len(text) > room:
                text = text[: max(room, 0)]
                truncated = True
            sent += len(text)
            if text:
                queue.put_nowait(Event("log", lane, text=text))

        start = time.monotonic()
        ok, error = True, ""
        try:
            await self._backend.execute(code, cold=lane is Lane.COLD, on_log=on_log)
        except asyncio.CancelledError:
            raise
        except RemoteExecutionError as exc:
            ok, error = False, exc.remote_traceback or str(exc)
        except ReadieError as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"
        except Exception:  # never leak internals to a visitor
            ok, error = False, "The playground hit an internal error."
        queue.put_nowait(
            Event(
                "done",
                lane,
                ok=ok,
                duration_s=round(time.monotonic() - start, 3),
                error=error,
                truncated=truncated,
            )
        )
