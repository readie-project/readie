import asyncio
import json

import httpx
import pytest
from starlette.types import Message, Scope

from readie_playground.app import create_app
from readie_playground.config import PlaygroundSettings
from readie_playground.runner import Runner
from tests.fakes import FakeBackend


def make_client(**overrides: object) -> httpx.AsyncClient:
    settings = PlaygroundSettings(**overrides)  # type: ignore[arg-type]
    app = create_app(settings, Runner(FakeBackend(), settings.max_output_chars))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_run_streams_both_lanes_as_sse() -> None:
    async with make_client() as client:
        res = await client.post("/run", json={"code": "print(1)"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert res.text.count("event: done") == 2
    assert '"lane": "optimized"' in res.text
    assert '"lane": "cold"' in res.text


@pytest.mark.parametrize(
    ("body", "status"),
    [({"code": "x" * 100}, 413), ({"code": "   "}, 422), ({}, 422)],
)
async def test_rejects_bad_input(body: dict[str, str], status: int) -> None:
    async with make_client(max_code_bytes=50) as client:
        res = await client.post("/run", json=body)
    assert res.status_code == status


async def test_repeated_requests_have_no_rate_limit() -> None:
    async with make_client() as client:
        for _ in range(12):
            assert (await client.post("/run", json={"code": "1"})).status_code == 200


async def test_cors_allows_only_the_configured_origin() -> None:
    async with make_client(allowed_origins=("https://readie.org",)) as client:
        ok = await client.options(
            "/run",
            headers={"Origin": "https://readie.org", "Access-Control-Request-Method": "POST"},
        )
        bad = await client.options(
            "/run",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        )
    assert ok.headers.get("access-control-allow-origin") == "https://readie.org"
    assert "access-control-allow-origin" not in bad.headers


async def test_healthz() -> None:
    async with make_client() as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}


@pytest.mark.parametrize("cold", [False, True])
async def test_remote_options_select_one_lane_and_reach_the_backend(cold: bool) -> None:
    backend = FakeBackend(output="hello\n")
    settings = PlaygroundSettings()
    app = create_app(settings, Runner(backend, settings.max_output_chars))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        res = await client.post(
            "/run",
            json={
                "code": "print('hello')",
                "memory": "8Gi",
                "max_memory": "16Gi",
                "timeout": 300,
                "session": "mine",
                "disable_optimized_execution": cold,
            },
        )
    assert res.status_code == 200
    assert res.text.count("event: done") == 1
    lane = "cold" if cold else "optimized"
    assert f'"lane": "{lane}"' in res.text
    assert '"text": "hello\\n"' in res.text
    assert backend.calls == []
    (call,) = backend.evals
    assert call.key == "mine"
    assert call.options.remote["memory"] == "8Gi"
    assert call.options.remote["max_memory"] == "16Gi"
    assert call.options.remote["timeout"] == 300
    assert call.options.remote["disable_optimized_execution"] is cold


async def test_an_explicit_null_option_selects_one_execution_with_sdk_defaults() -> None:
    backend = FakeBackend()
    app = create_app(PlaygroundSettings(), Runner(backend, 1000))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        res = await client.post("/run", json={"code": "x = 1", "memory": None})
    assert res.text.count("event: done") == 1
    (call,) = backend.evals
    assert call.options.remote["memory"] is None
    assert call.options.remote["timeout"] is None
    assert call.options.remote["session"] is None


@pytest.mark.parametrize(
    "options",
    [
        {"memory": "lots"},
        {"memory": "2Gi", "max_memory": "1Gi"},
        {"checkpoint_id": "checkpoint", "disable_optimized_execution": True},
        {"packages": ["--index-url=bad"]},
        {"unknown": True},
    ],
)
async def test_invalid_remote_options_are_rejected_before_streaming(
    options: dict[str, object],
) -> None:
    async with make_client() as client:
        res = await client.post("/run", json={"code": "print(1)", **options})
    assert res.status_code == 422


async def test_public_run_retains_the_concurrent_request_cap() -> None:
    async with make_client(max_concurrent_runs=0) as client:
        res = await client.post("/run", json={"code": "1", "memory": "2Gi"})
    assert res.status_code == 503


async def test_http_disconnect_cancels_execution_and_releases_the_public_slot() -> None:
    backend = FakeBackend(hang=True)
    app = create_app(PlaygroundSettings(max_concurrent_runs=1), Runner(backend, 1000))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/run",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }
    sent_request = False

    async def receive() -> Message:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {
                "type": "http.request",
                "body": json.dumps({"code": "while True: pass", "timeout": 30}).encode(),
                "more_body": False,
            }
        await backend.started.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        pass

    await asyncio.wait_for(app(scope, receive, send), timeout=2)
    assert backend.cancelled == 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        # Invalid Python ends without calling the hanging backend, after acquiring a slot.
        res = await client.post("/run", json={"code": "x = = 1"})
    assert res.status_code == 200
