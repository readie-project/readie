"""Stream one remote execution per request."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from readie import (
    Client,
    ReadieError,
    RemoteExecutionError,
    Session,
    Settings,
    remote,
)

from readie_playground import userfn


@dataclass(frozen=True, slots=True)
class Event:
    """One frame of the response stream."""

    kind: str  # "log" or "done"
    text: str = ""
    ok: bool = True
    duration_s: float = 0.0
    error: str = ""
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionOptions:
    """What a request passes to the Readie remote function."""

    remote: Mapping[str, Any] = field(default_factory=dict)
    """Keyword arguments of ``readie.remote``, exactly as the request gave them."""


class Backend(Protocol):
    """Where a run actually happens. The production implementation calls Readie."""

    async def execute(
        self,
        code: str,
        *,
        key: str,
        options: ExecutionOptions,
        on_log: Callable[[str], None],
    ) -> None:
        """Run ``code`` remotely with the request's ``remote`` arguments."""
        ...


class ReadieBackend:
    """Calls Readie through the client, the way a user's script would."""

    def __init__(self, settings: Settings, workdir: Path) -> None:
        self._settings = settings
        self._workdir = workdir

    async def execute(
        self,
        code: str,
        *,
        key: str,
        options: ExecutionOptions,
        on_log: Callable[[str], None],
    ) -> None:
        """Run ``code`` through ``readie.remote``, with the arguments as the caller sent them."""
        arguments = dict(options.remote)
        if arguments.get("session") is not None:
            # The request carries a session id; ``remote`` takes a ``Session``.
            arguments["session"] = Session(arguments["session"])
        # Module semantics, in a namespace that lasts for the session, so requests that
        # name the same session see each other's names.
        with userfn.loaded_step(code, key, 0, self._workdir) as main:
            async with Client(self._settings, log_sink=on_log) as client:
                remote_main = remote(client=client, **arguments)(main)
                await remote_main.aio()


class Runner:
    """Streams output and completion for one execution."""

    def __init__(
        self,
        backend: Backend,
        max_output_chars: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._max_output = max_output_chars
        self._clock = clock

    async def stream(
        self, code: str, options: ExecutionOptions | None = None
    ) -> AsyncGenerator[Event, None]:
        """Yield output followed by one completion event."""
        options = options or ExecutionOptions()
        error = userfn.check_step(code)
        if error is not None:
            yield Event("done", ok=False, error=error)
            return
        queue: asyncio.Queue[Event] = asyncio.Queue()
        task = asyncio.create_task(self._execute(code, queue, options))
        try:
            while True:
                event = await queue.get()
                yield event
                if event.kind == "done":
                    break
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _execute(
        self, code: str, queue: asyncio.Queue[Event], options: ExecutionOptions
    ) -> None:
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
                queue.put_nowait(Event("log", text=text))

        start = self._clock()
        ok, error = True, ""
        try:
            key = options.remote.get("session") or uuid.uuid4().hex
            await self._backend.execute(code, key=key, options=options, on_log=on_log)
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
                ok=ok,
                duration_s=round(self._clock() - start, 3),
                error=error,
                truncated=truncated,
            )
        )
