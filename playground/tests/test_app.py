import httpx
import pytest

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


async def test_rate_limit_returns_429() -> None:
    async with make_client(rate_limit_requests=1) as client:
        assert (await client.post("/run", json={"code": "1"})).status_code == 200
        assert (await client.post("/run", json={"code": "1"})).status_code == 429


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
