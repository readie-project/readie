"""Running a task through Readie and timing it.

Two targets. ``real`` submits the task to the local Readie stack over the client
and measures the comparison that is the point of the system: a **checkpoint** call
(the normal optimized path -- the router restores a checkpoint that already imported
the needed packages) against a **cold-start** call (``disable_optimized_execution``,
which tells the router to skip restore and start from scratch). Both are fresh
containers, so the difference is exactly what checkpoint restore saves.
``local`` runs the function in this process via ``RemoteFunction.local`` -- no
router, no gVisor -- as an offline dry run; because it executes here without a
sandbox, the runner restricts it to trusted, self-generated tasks, and only the
in-process time is available (there is no checkpoint/cold-start distinction).

A task with more than one code entry is a **session**: its cells run in order
against one warm container (a ``readie`` ``Session``), and the measurement is the
same pair -- ``checkpoint_seconds``/``cold_start_seconds`` -- but each is now the
*total* time for the whole sequence, taken in one session per side (checkpoint-
restored or cold-started) so both stay fresh containers save for the reuse between
a session's own cells.

**Repair happens in place, on both sides.** A cell (a session's, or a standalone
task's own single one -- both run through the same ``_run_cells`` loop) that
raises gets one repair attempt via the ``repair`` callback: on ``real`` the fixed
code retries in the *same session* as the failure, since the container that
raised is still warm, not a fresh one; on ``local`` there is no session to
preserve, so it just retries in-process. Either way the reported time for that
side is the failed attempt's own time plus the retry's, not just the retry --
the failed attempt genuinely happened. Checkpoint and cold-start are fully
independent runs of the *original* code (the router refuses to mix
``disable_optimized_execution`` within one already-checkpoint-restored session,
so they can never share one anyway): each starts from the task's own code and
packages, not from whatever the other side ended up with, and each gets its
own repair opportunity per cell. A bug that only one side actually hits (or
that only intermittently reproduces, e.g. a transient infra timeout) is not
silently hidden from the other side by an earlier fix. Their independence is
also what lets ``Executor.run`` run them *concurrently*, given a ``pool``: two
separate sessions, two separate containers, nothing shared between them.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import Executor as ThreadPoolLike
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import readie

from readie_evals.config import Settings

#: Cap on a stored result's repr, so a large return value does not bloat the results file.
_MAX_REPR = 200

#: Given a failing cell's own code and the error it raised, return the fixed
#: code and any additional packages it now needs, or ``None`` to give up (one
#: attempt per cell, per side). Deliberately code/error-only, not task-aware --
#: the runner binds task-level context (category, ...) into this shape.
RepairFn = Callable[[str, str, str], "tuple[str, tuple[str, ...]] | None"]

#: Given a wrapped remote function, return the zero-arg callable that actually
#: invokes it -- ``.local`` for the ``local`` target, ``.bind(session).__call__``
#: (via ``bind``) for ``real``. Lets ``_run_cells`` stay the same loop either way.
_CallMaker = Callable[[Any], Callable[[], Any]]


class Target(StrEnum):
    """Where a task runs."""

    REAL = "real"
    LOCAL = "local"


@dataclass(frozen=True, slots=True)
class ExecOutcome:
    """A successful run's measurements.

    ``checkpoint_seconds`` is the optimized path (checkpoint restore);
    ``cold_start_seconds`` is the same call with the optimization disabled (a true
    cold start). Their ratio is the speedup checkpoint restore buys. ``repaired``
    is whether either side needed an in-place fix to get there.
    """

    checkpoint_seconds: float | None
    cold_start_seconds: float | None
    value_repr: str
    payload_bytes: int | None
    repaired: bool = False


class TaskExecutionError(Exception):
    """A task raised while running. Carries what the runner needs to repair it."""

    def __init__(self, error_type: str, message: str, remote_traceback: str = "") -> None:
        """Record the error's type, message and traceback."""
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.message = message
        self.remote_traceback = remote_traceback


def _load_entrypoint(code: str, entrypoint: str) -> Any:
    namespace: dict[str, Any] = {"__name__": "__readie_eval_task__"}
    try:
        exec(code, namespace)  # noqa: S102 - defining the task module is the whole job
    except Exception as exc:
        raise TaskExecutionError(type(exc).__name__, str(exc), traceback.format_exc()) from exc
    func = namespace.get(entrypoint)
    if func is None:
        error_type = "NameError"
        message = f"module defines no {entrypoint!r}"
        raise TaskExecutionError(error_type, message)
    return func


def _as_failure(exc: BaseException) -> TaskExecutionError:
    if isinstance(exc, TaskExecutionError):
        return exc
    if isinstance(exc, readie.RemoteExecutionError):
        return TaskExecutionError(exc.remote_type, exc.remote_message, exc.remote_traceback)
    return TaskExecutionError(type(exc).__name__, str(exc), traceback.format_exc())


class Executor:
    """Builds a remote function from a task and runs it under a target.

    ``pool``, when given, bounds how many containers may be in flight at
    once across the whole run: ``run`` submits a task's checkpoint and
    cold-start sides to it and waits on both, rather than running them back
    to back. Callers (``Runner``) submit tasks to their own, separate pool --
    nesting the *same* pool would deadlock once every worker were a task
    blocked waiting on its own two container jobs, with none left free to
    run them.
    """

    def __init__(
        self, settings: Settings, *, target: Target, pool: ThreadPoolLike | None = None
    ) -> None:
        """Configure execution against ``target`` using ``settings``."""
        self._settings = settings
        self._target = target
        self._pool = pool
        self._configured = False
        self._configure_lock = threading.Lock()

    def _configure(self) -> None:
        if self._target is not Target.REAL or self._configured:
            return
        with self._configure_lock:
            if not self._configured:
                readie.configure(router_uri=self._settings.router_uri, tls=self._settings.tls)
                self._configured = True

    def _wrap(
        self,
        func: Any,
        memory: str | None,
        packages: tuple[str, ...],
        timeout: float | None,
        *,
        disable_optimized: bool,
    ) -> Any:
        deadline = timeout if timeout is not None else self._settings.call_timeout
        remote = readie.remote(
            memory=memory or self._settings.default_memory,
            packages=list(packages),
            timeout=deadline,
            gpu=False,
            disable_optimized_execution=disable_optimized,
        )
        return remote(func)

    def run(
        self,
        *,
        code: tuple[str, ...],
        memory: str | None,
        packages: tuple[str, ...],
        timeout: float | None,
        repair: RepairFn | None = None,
    ) -> ExecOutcome:
        """Measure the task under the target. Raises ``TaskExecutionError`` on failure.

        ``code`` is one or more cells. On ``real``: a session (one per side --
        checkpoint-restored, cold-started) runs every cell in order, repairing
        and retrying in place on a failure (see the module docstring). On
        ``local``: every cell runs in-process in order, with the same repair
        support minus the session (nothing to keep warm), and the total is
        reported as the checkpoint time.
        """
        self._configure()
        try:
            if self._target is Target.LOCAL:
                return self._run_local(code, memory, packages, timeout, repair)
            return self._run_real(code, memory, packages, timeout, repair)
        except Exception as exc:
            raise _as_failure(exc) from exc

    def _run_local(
        self,
        codes: tuple[str, ...],
        memory: str | None,
        packages: tuple[str, ...],
        timeout: float | None,
        repair: RepairFn | None,
    ) -> ExecOutcome:
        total, value, _codes, _packages, repaired, payload = self._run_cells(
            codes,
            memory,
            packages,
            timeout,
            disable_optimized=False,
            repair=repair,
            make_call=lambda wrapped: wrapped.local,
        )
        return ExecOutcome(total, None, _short_repr(value), payload, repaired=repaired)

    def _run_real(
        self,
        codes: tuple[str, ...],
        memory: str | None,
        packages: tuple[str, ...],
        timeout: float | None,
        repair: RepairFn | None,
    ) -> ExecOutcome:
        if self._pool is not None:
            ckpt_future = self._pool.submit(
                self._run_session,
                codes,
                memory,
                packages,
                timeout,
                disable_optimized=False,
                repair=repair,
            )
            cold_future = self._pool.submit(
                self._run_session,
                codes,
                memory,
                packages,
                timeout,
                disable_optimized=True,
                repair=repair,
            )
            checkpoint_seconds, _value, _ckpt_codes, _ckpt_packages, repaired_ckpt, payload1 = (
                ckpt_future.result()
            )
            cold_start_seconds, value, _codes, _packages, repaired_cold, payload2 = (
                cold_future.result()
            )
        else:
            checkpoint_seconds, _value, _ckpt_codes, _ckpt_packages, repaired_ckpt, payload1 = (
                self._run_session(
                    codes, memory, packages, timeout, disable_optimized=False, repair=repair
                )
            )
            cold_start_seconds, value, _codes, _packages, repaired_cold, payload2 = (
                self._run_session(
                    codes, memory, packages, timeout, disable_optimized=True, repair=repair
                )
            )
        payload = None
        if payload1 is not None or payload2 is not None:
            payload = (payload1 or 0) + (payload2 or 0)
        return ExecOutcome(
            checkpoint_seconds,
            cold_start_seconds,
            _short_repr(value),
            payload,
            repaired=repaired_ckpt or repaired_cold,
        )

    def _run_session(
        self,
        codes: tuple[str, ...],
        memory: str | None,
        packages: tuple[str, ...],
        timeout: float | None,
        *,
        disable_optimized: bool,
        repair: RepairFn | None,
    ) -> tuple[float, Any, tuple[str, ...], tuple[str, ...], bool, int | None]:
        client = readie.default_client()
        with client.session() as session:
            result = self._run_cells(
                codes,
                memory,
                packages,
                timeout,
                disable_optimized=disable_optimized,
                repair=repair,
                make_call=lambda wrapped: wrapped.bind(session),
            )
        # Not part of the measurement (taken above, before this): a container
        # this call's container pool slot was holding is only released here,
        # and the worker does not destroy or start pausing it the instant the
        # response comes back -- it goes idle first (SANDBOX_IDLE_TTL). Under
        # concurrency, freeing this slot immediately would let a new
        # container start before this one has even begun being reclaimed,
        # piling load onto the host instead of handing it back. Waiting past
        # the idle TTL first gives the reaper a chance to actually act on it.
        time.sleep(self._settings.container_release_wait)
        return result

    def _run_cells(
        self,
        codes: tuple[str, ...],
        memory: str | None,
        packages: tuple[str, ...],
        timeout: float | None,
        *,
        disable_optimized: bool,
        repair: RepairFn | None,
        make_call: _CallMaker,
    ) -> tuple[float, Any, tuple[str, ...], tuple[str, ...], bool, int | None]:
        """Run every cell in order, timing the total, one shared loop for every target.

        A cell that raises gets one repair attempt (if ``repair`` is given): the
        fixed code retries via the same ``make_call`` -- the same session on
        ``real``, since the container that raised is still warm -- and the
        sequence continues with whatever cells follow. ``repair`` failing, or
        its retry also raising, propagates the original failure; there is no
        second attempt at the same cell.

        Returns (total_seconds, last_value, final_codes, final_packages,
        repaired, payload_bytes).
        """
        total = 0.0
        value: Any = None
        final_codes = list(codes)
        current_packages = packages
        repaired = False
        payload_total = 0
        any_payload = False

        for i, code in enumerate(final_codes):
            elapsed, value, payload, failure = self._attempt(
                code,
                memory,
                current_packages,
                timeout,
                disable_optimized=disable_optimized,
                make_call=make_call,
            )
            total += elapsed
            if payload is not None:
                payload_total += payload
                any_payload = True
            if failure is None:
                continue
            if repair is None:
                raise failure
            fix = repair(code, failure.error_type, failure.message)
            if fix is None:
                raise failure
            fixed_code, extra_packages = fix
            current_packages = tuple(dict.fromkeys((*current_packages, *extra_packages)))

            retry_elapsed, value, retry_payload, retry_failure = self._attempt(
                fixed_code,
                memory,
                current_packages,
                timeout,
                disable_optimized=disable_optimized,
                make_call=make_call,
            )
            total += retry_elapsed
            if retry_payload is not None:
                payload_total += retry_payload
                any_payload = True
            if retry_failure is not None:
                raise retry_failure
            final_codes[i] = fixed_code
            repaired = True

        return (
            total,
            value,
            tuple(final_codes),
            current_packages,
            repaired,
            payload_total if any_payload else None,
        )

    def _attempt(
        self,
        code: str,
        memory: str | None,
        packages: tuple[str, ...],
        timeout: float | None,
        *,
        disable_optimized: bool,
        make_call: _CallMaker,
    ) -> tuple[float, Any, int | None, TaskExecutionError | None]:
        """Try one cell once. Never raises.

        Returns (elapsed_seconds, value, payload_bytes, failure): ``failure`` is
        ``None`` on success, else the other two are ``None``/best-effort.
        """
        start = time.perf_counter()
        try:
            func = _load_entrypoint(code, "task")
            payload = _payload_size(func)
            wrapped = self._wrap(
                func, memory, packages, timeout, disable_optimized=disable_optimized
            )
            value = make_call(wrapped)()
        except Exception as exc:  # noqa: BLE001 - classified into TaskExecutionError below
            return time.perf_counter() - start, None, None, _as_failure(exc)
        return time.perf_counter() - start, value, payload, None


def _short_repr(value: object) -> str:
    text = repr(value)
    return text if len(text) <= _MAX_REPR else text[: _MAX_REPR - 3] + "..."


def _payload_size(func: Any) -> int | None:
    try:
        import cloudpickle  # noqa: PLC0415

        return len(cloudpickle.dumps({"func": func, "args": (), "kwargs": {}}))
    except Exception:  # noqa: BLE001 - payload size is a nice-to-have, never fatal
        return None
