"""The MCP front end at /mcp: same admission and runner as /run, as agent tools."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from readie_playground.app import create_app
from readie_playground.config import PlaygroundSettings
from readie_playground.execution import ReadieExecutionRequest
from readie_playground.mcp_server import new_session_id
from readie_playground.runner import Runner
from tests.fakes import FakeBackend

HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "mcp-protocol-version": "2025-06-18",
}


class Mcp:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self._id = 0

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            message["params"] = params
        res = await self.client.post("/mcp", json=message, headers=HEADERS)
        assert res.status_code == 200, res.text
        body: dict[str, Any] = res.json()
        return body

    async def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        result: dict[str, Any] = (
            await self.rpc("tools/call", {"name": name, "arguments": arguments})
        )["result"]
        return result


@asynccontextmanager
async def serve(
    backend: FakeBackend | None = None, **overrides: Any
) -> AsyncIterator[tuple[Mcp, FakeBackend]]:
    backend = backend or FakeBackend()
    settings = PlaygroundSettings(**overrides)
    app = create_app(settings, Runner(backend, settings.max_output_chars))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            yield Mcp(client), backend


def text(result: dict[str, Any]) -> str:
    return str(result["content"][0]["text"])


async def test_run_python_takes_exactly_the_fields_of_the_run_request() -> None:
    async with serve() as (mcp, _):
        tools = (await mcp.rpc("tools/list"))["result"]["tools"]
    schema = next(t for t in tools if t["name"] == "run_python")["inputSchema"]
    assert set(schema["properties"]) == set(ReadieExecutionRequest.model_fields)
    assert schema["required"] == ["code"]


async def test_initialize_names_the_server_and_its_instructions() -> None:
    async with serve() as (mcp, _):
        result = (
            await mcp.rpc(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            )
        )["result"]
    assert result["serverInfo"]["name"] == "readie"
    assert "print()" in result["instructions"]


async def test_run_python_returns_printed_output() -> None:
    async with serve(FakeBackend(output="45\n")) as (mcp, backend):
        result = await mcp.call("run_python", code="print(45)", memory="2Gi")
    assert result["isError"] is False
    assert text(result).startswith("45\n---\nSucceeded in")
    assert backend.calls[0].options.remote["memory"] == "2Gi"


async def test_run_python_passes_the_session_like_run_does() -> None:
    async with serve() as (mcp, backend):
        await mcp.call("run_python", code="x = 1", session="sess-abc")
        await mcp.call("run_python", code="x = 1", session="")
    assert backend.calls[0].key == "sess-abc"
    assert backend.calls[0].options.remote["session"] == "sess-abc"
    # An empty id means no session, as on /run.
    assert backend.calls[1].options.remote["session"] is None


async def test_a_silent_success_says_only_printed_output_returns() -> None:
    async with serve() as (mcp, _):
        result = await mcp.call("run_python", code="1 + 1")
    assert result["isError"] is False
    assert "(no output)" in text(result)


async def test_a_failed_execution_is_a_tool_error_with_the_traceback() -> None:
    async with serve(FakeBackend(output="partial\n", fail_execution=True)) as (mcp, _):
        result = await mcp.call("run_python", code="raise ValueError")
    assert result["isError"] is True
    assert "partial" in text(result)
    assert "Traceback: ValueError" in text(result)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"code": "x" * 100}, "too large"),
        ({"code": "   "}, "Enter some code"),
        ({"code": "1", "memory": "1.5Gi"}, "1.5Gi"),
    ],
)
async def test_admission_rejections_are_tool_errors_and_nothing_runs(
    arguments: dict[str, Any], message: str
) -> None:
    async with serve(max_code_bytes=50) as (mcp, backend):
        result = await mcp.call("run_python", **arguments)
    assert result["isError"] is True
    assert message in text(result)
    assert backend.calls == []


async def test_run_and_mcp_share_one_rate_limit() -> None:
    async with serve(rate_limit_requests=1) as (mcp, _):
        assert (await mcp.client.post("/run", json={"code": "1"})).status_code == 200
        result = await mcp.call("run_python", code="1")
    assert result["isError"] is True
    assert "Too many runs" in text(result)


async def test_listing_tools_does_not_count_against_the_rate_limit() -> None:
    async with serve(rate_limit_requests=1) as (mcp, _):
        for _ in range(3):
            await mcp.rpc("tools/list")
        result = await mcp.call("run_python", code="1")
    assert result["isError"] is False


async def test_new_session_returns_fresh_ids_in_the_client_format() -> None:
    async with serve() as (mcp, _):
        first = text(await mcp.call("new_session"))
        second = text(await mcp.call("new_session"))
    assert first != second
    assert first.startswith("sess-")
    assert len(first) == len(new_session_id())
