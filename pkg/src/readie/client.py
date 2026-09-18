"""The client and its sessions."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from types import TracebackType
from typing import Any

from readie.budget import Budget
from readie.codec import CloudpickleCodec, ResultCodec
from readie.config import Settings
from readie.errors import BlockingCallInEventLoopError, ClientClosedError
from readie.identity import new_request_id, new_session_id
from readie.protocol import CallRef, Outcome, function_output, unwrap_envelope
from readie.resources import extract_imports
from readie.transport import AsyncGrpcTransport, AsyncTransport, GrpcTransport, Transport


class Session:
    """A handle on one warm container's Python state.

    Calls made under one session are routed to the same container, so state left
    behind by an earlier call -- an imported module, a loaded model, a fitted
    estimator -- is still there for the next.

    **Calls within a session run one at a time.** A container is a single Python
    interpreter behind a single unix socket and cannot serve two executions at
    once, so the router serialises them. Issuing ten calls concurrently on one
    session queues them; issuing them on ten sessions does not.

    Closing is local. It stops the client using the id; the router releases the
    container on its own TTL.
    """

    __slots__ = ("_closed", "id")

    def __init__(self, session_id: str | None = None) -> None:
        self.id = session_id or new_session_id()
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether this session has been closed."""
        return self._closed

    def close(self) -> None:
        """Stop using this session. Idempotent."""
        self._closed = True

    def _check(self) -> None:
        if self._closed:
            msg = f"session {self.id} is closed"
            raise ClientClosedError(msg)

    def __enter__(self) -> Session:
        """Enter the session scope."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the session on the way out."""
        self.close()

    def __repr__(self) -> str:
        """Show the id and whether it is still usable."""
        state = "closed" if self._closed else "open"
        return f"Session(id={self.id!r}, {state})"


class Client:
    """Submits calls to the router.

    Every collaborator is injected and defaulted, so a test supplies a fake
    transport and exercises the real client, and a deployment supplies its own
    codec without subclassing anything.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        codec: ResultCodec | None = None,
        transport: Transport | None = None,
        async_transport: AsyncTransport | None = None,
        log_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self._codec = codec or CloudpickleCodec()
        self._log_sink = log_sink if log_sink is not None else _default_log_sink
        self._closed = False

        # Transports are constructed eagerly but connect lazily, so building a
        # Client never touches the network and never needs a running loop.
        self._transport = transport if transport is not None else GrpcTransport(self.settings)
        self._async_transport = (
            async_transport if async_transport is not None else AsyncGrpcTransport(self.settings)
        )

    # -- sessions ----------------------------------------------------------
    @contextmanager
    def session(self, session_id: str | None = None) -> Iterator[Session]:
        """Open a session for the duration of the block.

        Read ``Session``'s docstring before using one: it buys warm state at the
        cost of serialising the calls inside it.
        """
        self._check_open()
        session = Session(session_id)
        try:
            yield session
        finally:
            session.close()

    # -- calls -------------------------------------------------------------
    def call(
        self,
        func: Callable[..., Any],
        args: Sequence[Any] = (),
        kwargs: Mapping[str, Any] | None = None,
        *,
        session: Session | None = None,
        timeout: float | None = None,
        budgets: tuple[Budget, ...] = (),
        gpu: bool = False,
        packages: tuple[str, ...] = (),
    ) -> Any:
        """Execute ``func`` remotely and return its result.

        Blocks. Raises ``BlockingCallInEventLoopError`` if called from a running
        loop, where blocking would stall the loop the response must arrive on.
        """
        _reject_running_loop()
        ref, payload, imports = self._prepare(func, args, kwargs, session, packages)
        outcome = self._transport.execute(
            ref,
            payload,
            imports,
            budgets,
            gpu=gpu,
            timeout=self._deadline(timeout),
            on_log=self._on_log(),
        )
        return self._finish(outcome)

    async def acall(
        self,
        func: Callable[..., Any],
        args: Sequence[Any] = (),
        kwargs: Mapping[str, Any] | None = None,
        *,
        session: Session | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - the deadline is the gRPC call's, not a wrapper's
        budgets: tuple[Budget, ...] = (),
        gpu: bool = False,
        packages: tuple[str, ...] = (),
    ) -> Any:
        """Execute ``func`` remotely and return its result, without blocking."""
        ref, payload, imports = self._prepare(func, args, kwargs, session, packages)
        outcome = await self._async_transport.execute(
            ref,
            payload,
            imports,
            budgets,
            gpu=gpu,
            timeout=self._deadline(timeout),
            on_log=self._on_log(),
        )
        return self._finish(outcome)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """Close the blocking channel. Idempotent."""
        self._closed = True
        self._transport.close()

    async def aclose(self) -> None:
        """Close both channels. Idempotent."""
        self._closed = True
        await self._async_transport.aclose()
        self._transport.close()

    def __enter__(self) -> Client:
        """Enter a scope that closes the client on exit."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the client."""
        self.close()

    async def __aenter__(self) -> Client:
        """Enter an async scope that closes the client on exit."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the client."""
        await self.aclose()

    def __repr__(self) -> str:
        """Show the router this client talks to."""
        return f"Client(router_uri={self.settings.router_uri!r})"

    # -- internals ---------------------------------------------------------
    def _prepare(
        self,
        func: Callable[..., Any],
        args: Sequence[Any],
        kwargs: Mapping[str, Any] | None,
        session: Session | None,
        packages: tuple[str, ...] = (),
    ) -> tuple[CallRef, bytes, tuple[str, ...]]:
        self._check_open()
        if session is not None:
            session._check()

        # A call with no session sends no session_id at all, rather than a
        # fresh one per call: the router treats an empty id as "not part of
        # any session" and skips session bookkeeping for it entirely, so
        # nothing accumulates for the vast majority of calls that were never
        # going to be resumed anyway.
        session_id = session.id if session is not None else ""
        ref = CallRef(request_id=new_request_id(), session_id=session_id)
        payload = self._codec.encode_call(func, args, kwargs or {}, packages)
        imports = extract_imports(func)
        return ref, payload, imports

    def _finish(self, outcome: Outcome) -> Any:
        envelope = self._codec.decode_result(outcome.payload)
        if self.settings.stream_logs:
            for text in function_output(envelope):
                self._log_sink(text)
        return unwrap_envelope(envelope, outcome)

    def _deadline(self, timeout: float | None) -> float | None:
        return timeout if timeout is not None else self.settings.timeout

    def _on_log(self) -> Callable[[str], None] | None:
        return self._log_sink if self.settings.stream_logs else None

    def _check_open(self) -> None:
        if self._closed:
            msg = "client is closed"
            raise ClientClosedError(msg)


def _default_log_sink(line: str) -> None:
    """Write executor output to stderr.

    stderr, not stdout: these are another process's diagnostics, and a script
    that pipes its own stdout should not find them interleaved in the data.
    """
    print(line, file=sys.stderr, end="" if line.endswith("\n") else "\n")  # noqa: T201


def _reject_running_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    msg = (
        "a blocking readie call was made from a running event loop, which would "
        "stall it; await the .aio(...) form instead"
    )
    raise BlockingCallInEventLoopError(msg)
