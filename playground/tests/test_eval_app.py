import inspect

import httpx
import pytest
from readie import remote

from readie_playground.app import EvalRequest, create_app
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
    return await client.post("/eval/run", json=body)


async def test_the_route_is_served_without_a_token() -> None:
    # There is no token: nginx is what keeps the route off the public path (see nginx/).
    client, backend = make()
    async with client:
        res = await post(client, {"code": "a = 1"})
    assert res.status_code == 200
    assert len(backend.evals) == 1


async def test_a_run_returns_one_flat_result() -> None:
    client, backend = make(FakeBackend(output="hello\n"))
    async with client:
        res = await post(client, {"code": "print('hello')"})

    body = res.json()
    assert res.status_code == 200
    assert set(body) == {
        "session_id", "ok", "duration_s", "error", "error_type", "output", "truncated"
    }  # fmt: skip
    assert "lanes" not in body
    assert "steps" not in body
    assert body["ok"] is True
    assert isinstance(body["duration_s"], float)
    assert body["output"] == "hello\n"
    assert body["session_id"] is None
    assert backend.evals[0].source == "print('hello')"


async def test_a_failure_is_reported_in_the_response_not_as_an_http_error() -> None:
    client, _ = make(FakeBackend(fail_eval=True))
    async with client:
        res = await post(client, {"code": "a = 1"})

    body = res.json()
    assert res.status_code == 200
    assert body["ok"] is False
    assert body["error_type"] == "RemoteExecutionError"


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

    assert res.json()["session_id"] == "abc"
    (call,) = backend.evals
    assert dict(call.options.remote) == sent


async def test_an_empty_session_means_no_session() -> None:
    # Pinned: "" once reached Session(""), which invents a random id, so the call became a
    # tracked session with a warm container that no later request could name.
    client, backend = make()
    async with client:
        res = await post(client, {"code": "a = 1", "session": ""})

    assert res.json()["session_id"] is None
    (call,) = backend.evals
    assert call.options.remote["session"] is None


async def test_the_route_accepts_exactly_the_arguments_of_remote() -> None:
    # Every parameter of @remote (except the process-local `client`) is a field.
    remote_args = set(inspect.signature(remote).parameters) - {"func", "client"}
    route_args = set(EvalRequest.model_fields) - {"code"}
    assert route_args == remote_args


async def test_without_arguments_nothing_is_decided_on_the_callers_behalf() -> None:
    client, backend = make(timeout_s=45.0)
    async with client:
        await post(client, {"code": "a = 1"})

    (call,) = backend.evals
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
    assert backend.evals == []


async def test_large_values_are_not_capped_by_the_route() -> None:
    client, backend = make()
    async with client:
        res = await post(client, {"code": "a = 1", "memory": "512Gi", "timeout": 99999})

    assert res.status_code == 200
    (call,) = backend.evals
    assert (call.options.remote["memory"], call.options.remote["timeout"]) == ("512Gi", 99999.0)


async def test_too_much_code_is_refused() -> None:
    client, _ = make(eval_max_code_bytes=10)
    async with client:
        res = await post(client, {"code": "x" * 11})
    assert res.status_code == 413


async def test_the_limit_is_larger_than_the_public_playgrounds() -> None:
    # The public /run limit is 16 KiB; an evaluation sends whole notebooks.
    client, _ = make()
    async with client:
        res = await post(client, {"code": "x = 1\n" * 10_000})
    assert res.status_code == 200


async def test_the_public_run_route_is_unchanged_and_needs_no_token() -> None:
    client, _ = make()
    async with client:
        res = await client.post("/run", json={"code": "print(1)"})
    assert res.status_code == 200
    assert res.text.count("event: done") == 2
