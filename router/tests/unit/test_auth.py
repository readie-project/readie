"""The ProxyService bearer-token guard."""

from __future__ import annotations

import pytest

from readie_router.grpcserver.auth import AuthInterceptor, SharedTokenAuthenticator


def test_shared_token_accepts_the_configured_token() -> None:
    auth = SharedTokenAuthenticator("s3cret")
    assert auth.authorize([("authorization", "Bearer s3cret")])
    assert auth.authorize([("authorization", "bearer s3cret")]), "scheme is case-insensitive"
    assert auth.authorize([("authorization", "s3cret")]), "a bare token is accepted too"
    assert auth.authorize([("authorization", b"Bearer s3cret")]), "bytes metadata is handled"


def test_shared_token_rejects_wrong_missing_or_elsewhere() -> None:
    auth = SharedTokenAuthenticator("s3cret")
    assert not auth.authorize([("authorization", "Bearer nope")])
    assert not auth.authorize([])
    assert not auth.authorize([("x-other-header", "s3cret")])


def test_an_empty_token_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        SharedTokenAuthenticator("")


class _Details:
    """A stand-in for grpc.HandlerCallDetails."""

    def __init__(self, method: str, metadata: object) -> None:
        self.method = method
        self.invocation_metadata = metadata


async def _continuation(_details: object) -> str:
    return "REAL_HANDLER"


async def test_non_proxy_methods_are_never_guarded() -> None:
    # Workers, health and reflection must work with no token at all.
    interceptor = AuthInterceptor(SharedTokenAuthenticator("s3cret"))
    for method in ("/RegistryService/PostWorkerStatus", "/grpc.health.v1.Health/Check"):
        handler = await interceptor.intercept_service(_continuation, _Details(method, ()))
        assert handler == "REAL_HANDLER"


async def test_a_proxy_call_with_the_token_is_allowed_through() -> None:
    interceptor = AuthInterceptor(SharedTokenAuthenticator("s3cret"))
    handler = await interceptor.intercept_service(
        _continuation,
        _Details("/ProxyService/RequestExecution", [("authorization", "Bearer s3cret")]),
    )
    assert handler == "REAL_HANDLER"


async def test_a_proxy_call_without_the_token_is_denied() -> None:
    interceptor = AuthInterceptor(SharedTokenAuthenticator("s3cret"))
    handler = await interceptor.intercept_service(
        _continuation, _Details("/ProxyService/RequestExecution", [])
    )
    assert handler != "REAL_HANDLER", "a deny handler is substituted, not the real one"
    assert handler.stream_stream is not None, "it matches RequestExecution's cardinality"
