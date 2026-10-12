"""Run code on Readie twice, in parallel: once with checkpoint restore, once without.

The two lanes differ only in ``disable_optimized_execution``: the ``optimized``
lane uses checkpoint restore, the ``cold`` lane does not. Each lane builds the
script into a ``@remote`` function (``userfn``) and calls it through the Readie
client, as any user would.

``Runner.stream`` serves the public playground: one script, both lanes streamed.
``Runner.run_eval`` serves an evaluation: a list of steps run one after another
in each lane, under one session id per lane, with per-step timings.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from readie import Client, ReadieError, RemoteExecutionError, RemoteFunction, Session, Settings
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


@dataclass(frozen=True, slots=True)
class EvalOptions:
    """What an evaluation passes to the Readie remote function for every step."""

    memory: str = MEMORY
    max_memory: str = MEMORY
    gpu: bool = False
    gpu_memory: str | None = None
    max_gpu_memory: str | None = None
    packages: tuple[str, ...] = ()
    timeout_s: float = 30.0
    """Deadline of each step."""
    total_timeout_s: float | None = None
    """Budget for all the steps of one lane together; ``None`` for no budget."""
    continue_on_error: bool = True


@dataclass(frozen=True, slots=True)
class StepResult:
    """How one step of one lane went.

    ``duration_s`` is measured exactly as for the playground: a monotonic clock
    around the whole backend call (building the function, connecting, the call).
    """

    index: int
    ok: bool
    duration_s: float
    error: str = ""
    error_type: str = ""
    output: str = ""
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class LaneResult:
    """Every step of one lane."""

    lane: Lane
    session_id: str | None
    steps: tuple[StepResult, ...]

    @property
    def total_s(self) -> float:
        """Sum of the step durations."""
        return round(sum(step.duration_s for step in self.steps), 3)

    @property
    def ok(self) -> bool:
        """Whether every step succeeded."""
        return all(step.ok for step in self.steps)


@dataclass(frozen=True, slots=True)
class EvalOutcome:
    """Both lanes of one evaluation request."""

    session_id: str | None
    lanes: dict[Lane, LaneResult]


class Backend(Protocol):
    """Where a run actually happens. The production implementation calls Readie."""

    async def execute(self, code: str, *, cold: bool, on_log: Callable[[str], None]) -> None:
        """Run ``code`` remotely, calling ``on_log`` with output as it arrives."""
        ...

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
        """Run one step remotely under ``session_id`` (``None``: no session)."""
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
        """Run one step through the client, with every option the caller passed."""
        budgets = build_budgets(
            memory=options.memory,
            max_memory=options.max_memory,
            gpu_memory=options.gpu_memory,
            max_gpu_memory=options.max_gpu_memory,
        )
        with userfn.loaded_step(source, key, index, self._workdir) as main:
            async with Client(self._settings, log_sink=on_log) as client:
                remote_main = RemoteFunction(
                    main,
                    client=client,
                    budgets=budgets,
                    gpu=options.gpu,
                    packages=options.packages,
                    timeout=options.timeout_s,
                    session=Session(session_id) if session_id is not None else None,
                    disable_optimized_execution=cold,
                )
                await remote_main.aio()


class Runner:
    """Fans one request out to both lanes and merges their events."""

    def __init__(
        self,
        backend: Backend,
        max_output_chars: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._max_output = max_output_chars
        self._clock = clock

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

        start = self._clock()
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
                duration_s=round(self._clock() - start, 3),
                error=error,
                truncated=truncated,
            )
        )

    async def run_eval(
        self, steps: list[str], options: EvalOptions, *, session: str | None = None
    ) -> EvalOutcome:
        """Run ``steps`` in both lanes at once, each lane's steps one after another.

        More than one step, or an explicit ``session``, runs each lane under its own
        session id (``<id>-optimized`` and ``<id>-cold``) so the steps share one
        container and one interpreter. A single step with no session id is a plain
        call whose container is released at once. The lanes never share a session,
        because an optimized session cannot be asked to start cold.
        """
        base = session or (uuid.uuid4().hex if len(steps) > 1 else None)
        run_key = uuid.uuid4().hex
        lanes = await asyncio.gather(
            *(
                self._eval_lane(
                    lane,
                    steps,
                    options,
                    session_id=f"{base}-{lane.value}" if base else None,
                    key=f"{run_key}-{lane.value}",
                )
                for lane in Lane
            )
        )
        return EvalOutcome(base, {result.lane: result for result in lanes})

    async def _eval_lane(
        self,
        lane: Lane,
        steps: list[str],
        options: EvalOptions,
        *,
        session_id: str | None,
        key: str,
    ) -> LaneResult:
        results: list[StepResult] = []
        spent = 0.0
        for index, source in enumerate(steps):
            if results and not results[-1].ok and not options.continue_on_error:
                results.append(
                    StepResult(index, False, 0.0, "skipped after an earlier step failed", "Skipped")
                )
                continue
            step_options = options
            if options.total_timeout_s is not None:
                left = options.total_timeout_s - spent
                if left <= 0:
                    results.append(
                        StepResult(
                            index,
                            False,
                            0.0,
                            "the time budget for the item was used up",
                            "OutOfTime",
                        )
                    )
                    continue
                step_options = replace(options, timeout_s=min(options.timeout_s, left))
            result = await self._eval_step(
                lane, source, index, options=step_options, session_id=session_id, key=key
            )
            spent += result.duration_s
            results.append(result)
        return LaneResult(lane, session_id, tuple(results))

    async def _eval_step(
        self,
        lane: Lane,
        source: str,
        index: int,
        *,
        options: EvalOptions,
        session_id: str | None,
        key: str,
    ) -> StepResult:
        syntax = userfn.check_step(source)
        if syntax is not None:
            # Not valid Python: report it without a call, which would only time a failure.
            return StepResult(index, False, 0.0, syntax, "SyntaxError")

        chunks: list[str] = []
        sent = 0
        truncated = False

        def on_log(text: str) -> None:
            nonlocal sent, truncated
            if truncated:
                return
            room = self._max_output - sent
            if len(text) > room:
                text, truncated = text[: max(room, 0)], True
            sent += len(text)
            chunks.append(text)

        start = self._clock()
        ok, error, error_type = True, "", ""
        try:
            await self._backend.execute_step(
                source,
                index=index,
                key=key,
                session_id=session_id,
                cold=lane is Lane.COLD,
                options=options,
                on_log=on_log,
            )
        except asyncio.CancelledError:
            raise
        except RemoteExecutionError as exc:
            ok, error, error_type = False, exc.remote_traceback or str(exc), "RemoteExecutionError"
        except ReadieError as exc:
            ok, error, error_type = False, f"{type(exc).__name__}: {exc}", type(exc).__name__
        except Exception as exc:
            ok, error, error_type = False, f"{type(exc).__name__}: {exc}", "InternalError"
        return StepResult(
            index,
            ok,
            round(self._clock() - start, 3),
            error,
            error_type,
            "".join(chunks),
            truncated,
        )
