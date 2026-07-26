"""A real gRPC server and a real client, on an ephemeral port."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from crfs.client import Client
from crfs.config import Settings
from tests.fakes.router import RouterHarness


@pytest_asyncio.fixture
async def harness() -> AsyncIterator[RouterHarness]:
    harness = RouterHarness()
    await harness.start()
    try:
        yield harness
    finally:
        await harness.stop()


@pytest_asyncio.fixture
async def client(harness: RouterHarness) -> AsyncIterator[Client]:
    client = Client(Settings(router_uri=harness.uri, timeout=15.0, stream_logs=False))
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def logged() -> list[str]:
    return []
