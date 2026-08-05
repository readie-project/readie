"""Authorization for the client-facing request path.

By default the router speaks plaintext with no auth (see SECURITY.md). When an
auth token is configured, a bearer token is **required on ``ProxyService`` calls**
— the only path that runs code — so reaching the port is no longer enough to
execute on the cluster. ``RegistryService`` (workers), health and reflection are
exempt: the worker mesh is trusted, and the compose healthcheck must keep working
without a token.

``Authenticator`` is a ``Protocol`` so a deployment can replace the shared-token
check with JWT or per-tenant validation without touching the interceptor — the
same seam philosophy as ``ResultCodec`` on the client.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, runtime_checkable

import grpc

#: Only calls to this service run user code, so it is the only one guarded.
GUARDED_PREFIX = "/ProxyService/"
_METADATA_KEY = "authorization"
_BEARER = "bearer "

# gRPC invocation metadata: a sequence of (key, value) with str or bytes values.
Metadata = Sequence[tuple[str, str | bytes]]


@runtime_checkable
class Authenticator(Protocol):
    """Decides whether a call's metadata authorizes it."""

    def authorize(self, metadata: Metadata) -> bool:
        """Return whether the presented metadata is authorized."""
        ...


class SharedTokenAuthenticator:
    """Accepts a single shared bearer token, compared in constant time.

    The comparison is ``hmac.compare_digest`` rather than ``==`` so a caller
    cannot recover the token a byte at a time from response timing.
    """

    def __init__(self, token: str) -> None:
        if not token:
            msg = "a SharedTokenAuthenticator needs a non-empty token"
            raise ValueError(msg)
        self._token = token

    def authorize(self, metadata: Metadata) -> bool:
        """Return whether ``authorization: Bearer <token>`` matches."""
        for key, value in metadata:
            if key.lower() != _METADATA_KEY:
                continue
            presented = value.decode() if isinstance(value, bytes) else value
            if presented.lower().startswith(_BEARER):
                presented = presented[len(_BEARER) :]
            return hmac.compare_digest(presented, self._token)
        return False


class AuthInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]  # grpc stub is Any
    """Rejects unauthorized ``ProxyService`` calls with UNAUTHENTICATED."""

    def __init__(self, authenticator: Authenticator) -> None:
        self._auth = authenticator

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        """Guard ProxyService; pass everything else straight through."""
        method = handler_call_details.method
        if not method.startswith(GUARDED_PREFIX):
            return await continuation(handler_call_details)

        metadata = handler_call_details.invocation_metadata or ()
        if self._auth.authorize(metadata):
            return await continuation(handler_call_details)
        return _deny_handler()


def _deny_handler() -> grpc.RpcMethodHandler:
    """A handler that aborts with UNAUTHENTICATED.

    ProxyService.RequestExecution is bidirectional streaming, so the deny handler
    is stream-stream to match; a mismatched cardinality would confuse the client
    before it saw the status.
    """

    async def abort(_request_iterator: object, context: grpc.aio.ServicerContext) -> None:
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "a valid bearer token is required")

    return grpc.stream_stream_rpc_method_handler(abort)
