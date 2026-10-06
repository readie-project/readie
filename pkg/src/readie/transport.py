"""Transports: the only modules that speak gRPC.

Two of them, sync and async, over the same ``RequestEncoder`` and
``ResponseAssembler``. They are separate implementations rather than one async
core with a thread bridge because gRPC provides both APIs natively, and a
blocking call that secretly runs a private event loop on a background thread is
a debugging problem the moment anything goes wrong.

Both are ``Protocol``-shaped seams, so the tests substitute in-memory doubles
and the client under test is the real one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

import grpc

from readie import _status
from readie._channels import AsyncChannelCache, SyncChannelCache
from readie._proto import proxy_pb2, proxy_pb2_grpc
from readie.protocol import CallRef, Outcome, RequestEncoder, ResponseAssembler

if TYPE_CHECKING:
    from collections.abc import Callable

    from readie.budget import Budget
    from readie.config import Settings


def _auth_metadata(settings: Settings) -> list[tuple[str, str]] | None:
    """Bearer-token call metadata for a router that requires one, or ``None``.

    Sent as ordinary metadata, not gRPC call credentials, so it works over a
    plaintext channel too (gRPC forbids call credentials on an insecure channel).
    """
    if not settings.auth_token:
        return None
    return [("authorization", f"Bearer {settings.auth_token}")]


@runtime_checkable
class Transport(Protocol):
    """Executes a call and blocks until it finishes."""

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
        disable_optimized_execution: bool = False,
    ) -> Outcome:
        """Run one call to completion."""
        ...

    def close(self) -> None:
        """Release the underlying connection."""
        ...


@runtime_checkable
class AsyncTransport(Protocol):
    """Executes a call on an event loop."""

    async def execute(
        self,
        ref: CallRef,
        payload: bytes,
        imports: tuple[str, ...],
        budgets: tuple[Budget, ...],
        *,
        timeout: float | None,  # noqa: ASYNC109 - the deadline is the gRPC call's, not a wrapper's
        on_log: Callable[[str], None] | None,
        gpu: bool = False,
        disable_optimized_execution: bool = False,
    ) -> Outcome:
        """Run one call to completion."""
        ...

    async def aclose(self) -> None:
        """Release the underlying connection."""
        ...


class GrpcTransport:
    """Blocking transport over ``grpc.Channel``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._channels = SyncChannelCache(settings)
        self._encoder = RequestEncoder(chunk_size=settings.chunk_size)
        self._metadata = _auth_metadata(settings)

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
        disable_optimized_execution: bool = False,
    ) -> Outcome:
        """Stream the call to the router and assemble the response."""
        stub = proxy_pb2_grpc.ProxyServiceStub(self._channels.get())
        assembler = ResponseAssembler(on_log=on_log)
        requests = self._encoder.encode(
            ref,
            payload,
            imports,
            budgets,
            gpu=gpu,
            disable_optimized_execution=disable_optimized_execution,
        )

        call = stub.RequestExecution(requests, timeout=timeout, metadata=self._metadata)
        try:
            for response in call:
                assembler.accept(response)
        except grpc.RpcError as exc:
            raise _status.translate(exc, target=self._settings.router_uri) from exc
        except BaseException:
            # Ctrl-C, or a log callback that raised. Tell the router rather than
            # leaving it holding a lease until the TTL sweeps it.
            call.cancel()
            raise

        return Outcome(
            payload=assembler.result(),
            logs=assembler.logs,
            attribution=assembler.attribution,
        )

    def close(self) -> None:
        """Close the channel."""
        self._channels.close()


class AsyncGrpcTransport:
    """Transport over ``grpc.aio.Channel``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._channels = AsyncChannelCache(settings)
        self._encoder = RequestEncoder(chunk_size=settings.chunk_size)
        self._metadata = _auth_metadata(settings)

    async def execute(
        self,
        ref: CallRef,
        payload: bytes,
        imports: tuple[str, ...],
        budgets: tuple[Budget, ...],
        *,
        timeout: float | None,  # noqa: ASYNC109 - the deadline is the gRPC call's, not a wrapper's
        on_log: Callable[[str], None] | None,
        gpu: bool = False,
        disable_optimized_execution: bool = False,
    ) -> Outcome:
        """Stream the call to the router and assemble the response."""
        stub = proxy_pb2_grpc.ProxyServiceStub(self._channels.get())
        assembler = ResponseAssembler(on_log=on_log)

        # The request iterator is synchronous and grpc.aio accepts that: chunking
        # an in-memory buffer never blocks, so an async generator would add a
        # scheduling hop per megabyte and buy nothing.
        requests = self._encoder.encode(
            ref,
            payload,
            imports,
            budgets,
            gpu=gpu,
            disable_optimized_execution=disable_optimized_execution,
        )

        call = stub.RequestExecution(requests, timeout=timeout, metadata=self._metadata)
        try:
            async for response in call:
                assembler.accept(response)
        except grpc.aio.AioRpcError as exc:
            raise _status.translate(exc, target=self._settings.router_uri) from exc
        except BaseException:
            # Includes CancelledError: the caller's task was cancelled, and the
            # router must learn that now rather than when its lease expires.
            call.cancel()
            raise

        return Outcome(
            payload=assembler.result(),
            logs=assembler.logs,
            attribution=assembler.attribution,
        )

    async def aclose(self) -> None:
        """Close the running loop's channel."""
        await self._channels.close()


def response(
    *,
    request_id: str = "",
    session_id: str = "",
    success: bool = True,
    payload: bytes | None = None,
    logs: str | None = None,
) -> proxy_pb2.ClientExecutionResponse:
    """Build a response message. Exported for tests and fake routers."""
    message = proxy_pb2.ClientExecutionResponse(
        request_id=request_id, session_id=session_id, success=success
    )
    if payload is not None:
        message.payload = payload
    elif logs is not None:
        message.logs = logs
    return message
