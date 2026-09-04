"""The bearer token reaches the router over a real gRPC channel."""

from __future__ import annotations

import os

from readie.client import Client
from readie.config import Settings
from tests.fakes.router import RouterHarness


def add(a: int, b: int) -> int:
    return a + b


async def test_the_auth_token_is_sent_as_bearer_metadata(harness: RouterHarness) -> None:
    os.environ["READIE_AUTH_TOKEN"] = "s3cret" # noqa: S106 - a test literal, not a secret
    client = Client(
        Settings(
            router_uri=harness.uri,
            timeout=15.0,
            tls=False,
            stream_logs=False,
        )
    )
    try:
        assert await client.acall(add, (1, 2)) == 3
    finally:
        await client.aclose()
        del os.environ["READIE_AUTH_TOKEN"]

    assert ("authorization", "Bearer s3cret") in harness.router.metadata


async def test_no_token_sends_no_authorization_header(harness: RouterHarness) -> None:
    client = Client(Settings(router_uri=harness.uri, timeout=15.0, tls=False, stream_logs=False))
    try:
        assert await client.acall(add, (2, 2)) == 4
    finally:
        await client.aclose()

    assert not any(key == "authorization" for key, _ in harness.router.metadata)
