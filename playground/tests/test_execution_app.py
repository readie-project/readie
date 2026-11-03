import inspect

import httpx
import pytest
from readie import remote

from readie_playground.app import ReadieExecutionRequest, create_app
from readie_playground.config import PlaygroundSettings
from readie_playground.runner import Runner
from tests.fakes import FakeBackend


def make(
    backend: FakeBackend | None = None, **overrides: object
) -> tuple[httpx.AsyncClient, FakeBackend]:
    backend = backend or FakeBackend()
    settings = PlaygroundSettings(**overrides)  # type: ignore[arg-type]
    app = create_app(settings, Runner(backend, settings.max_output_chars))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"), backend


async def post(client: httpx.AsyncClient, body: dict[str, object]) -> httpx.Response:
    return await client.post("/run", json=body)


async def test_the_route_is_served_without_a_token() -> None:
    # The public HTTP execution route does not require a token.
    client, backend = make()
    async with client:
        res = await post(client, {"code": "a = 1"})
    assert res.status_code == 200
    assert len(backend.calls) == 1


async def test_every_remote_argument_is_passed_through_untouched() -> None:
    # Spelled as @remote spells them. Pinned so a new @remote argument is not
    # silently dropped here (see AGENTS.md).
    sent = {
        "timeout": 300,
        "session": "abc",
        "gpu": True,
        "memory": "8Gi",
        "max_memory": "16Gi",
        "gpu_memory": "4Gi",
        "max_gpu_memory": "8Gi",
        "packages": ["lightgbm==4.*", "numpy"],
        "disable_optimized_execution": False,
        "checkpoint_id": "checkpoint_3",
    }
    client, backend = make()
    async with client:
        res = await post(client, {"code": "a = 1", **sent})

    assert res.status_code == 200
    (call,) = backend.calls
    assert dict(call.options.remote) == sent


async def test_an_empty_session_means_no_session() -> None:
    # Pinned: "" once reached Session(""), which invents a random id, so the call became a
    # tracked session with a warm container that no later request could name.
    client, backend = make()
    async with client:
        res = await post(client, {"code": "a = 1", "session": ""})

    assert res.status_code == 200
    (call,) = backend.calls
    assert call.options.remote["session"] is None


async def test_the_route_accepts_exactly_the_arguments_of_remote() -> None:
    # Every parameter of @remote (except the process-local `client`) is a field.
    remote_args = set(inspect.signature(remote).parameters) - {"func", "client"}
    route_args = set(ReadieExecutionRequest.model_fields) - {"code"}
    assert route_args == remote_args


async def test_without_arguments_nothing_is_decided_on_the_callers_behalf() -> None:
    client, backend = make()
    async with client:
        await post(client, {"code": "a = 1"})

    (call,) = backend.calls
    # remote's own defaults apply: no memory, no timeout, no session.
    assert call.options.remote["memory"] is None
    assert call.options.remote["timeout"] is None
    assert call.options.remote["session"] is None


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"code": ""}, 422),
        ({"code": "   \n"}, 422),
        ({"code": "a = 1", "memory": "lots"}, 422),
        ({"code": "a = 1", "memory": "2Gi", "max_memory": "1Gi"}, 422),
        ({"code": "a = 1", "packages": ["not a requirement!!"]}, 422),
        # @remote rejects this pair itself, and the route does not second-guess it.
        ({"code": "a = 1", "checkpoint_id": "c", "disable_optimized_execution": True}, 422),
        # Fields that are no longer part of the route are refused, not ignored.
        ({"code": "a = 1", "timeout_s": 30}, 422),
        ({"code": "a = 1", "total_timeout_s": 30}, 422),
        ({"code": "a = 1", "continue_on_error": True}, 422),
        ({"steps": ["a = 1"]}, 422),
        ({"code": ["a = 1"]}, 422),
        ({}, 422),
    ],
)
async def test_bad_requests_are_rejected_before_anything_runs(
    body: dict[str, object], status: int
) -> None:
    client, backend = make()
    async with client:
        res = await post(client, body)
    assert res.status_code == status
    assert backend.calls == []


async def test_large_values_are_not_capped_by_the_route() -> None:
    client, backend = make()
    async with client:
        res = await post(client, {"code": "a = 1", "memory": "512Gi", "timeout": 99999})

    assert res.status_code == 200
    (call,) = backend.calls
    assert (call.options.remote["memory"], call.options.remote["timeout"]) == ("512Gi", 99999.0)


async def test_too_much_code_is_refused() -> None:
    client, _ = make(max_code_bytes=10)
    async with client:
        res = await post(client, {"code": "x" * 11})
    assert res.status_code == 413


async def test_retired_evaluation_route_returns_404() -> None:
    client, backend = make()
    async with client:
        response = await client.post("/eval/run", json={"code": "print(1)"})
    assert response.status_code == 404
    assert backend.calls == []
