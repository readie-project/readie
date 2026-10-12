import asyncio

import httpx
import pytest

from readie_playground.app import create_app
from readie_playground.config import PlaygroundSettings
from readie_playground.runner import Runner
from tests.fakes import FakeBackend

TOKEN = "s3cret"  # noqa: S105 - a test value
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make(
    backend: FakeBackend | None = None, **overrides: object
) -> tuple[httpx.AsyncClient, FakeBackend]:
    backend = backend or FakeBackend()
    settings = PlaygroundSettings(**{"eval_token": TOKEN, **overrides})  # type: ignore[arg-type]
    app = create_app(settings, Runner(backend, settings.max_output_chars))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"), backend


async def post(
    client: httpx.AsyncClient, body: dict[str, object], headers: dict[str, str] = AUTH
) -> httpx.Response:
    return await client.post("/eval/run", json=body, headers=headers)


async def test_the_route_does_not_exist_without_a_configured_token() -> None:
    client, _ = make(eval_token="")
    async with client:
        res = await post(client, {"steps": ["a = 1"]})
    assert res.status_code == 404


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
    ],
)
async def test_a_missing_or_wrong_token_is_refused_before_anything_runs(
    headers: dict[str, str],
) -> None:
    client, backend = make()
    async with client:
        res = await post(client, {"steps": ["a = 1"]}, headers=headers)
    assert res.status_code == 401
    assert backend.steps == []


async def test_a_run_returns_both_lanes_with_a_duration_per_step() -> None:
    client, _ = make()
    async with client:
        res = await post(client, {"steps": ["a = 1", "b = 2"]})

    body = res.json()
    assert res.status_code == 200
    assert set(body["lanes"]) == {"optimized", "cold"}
    for lane in body["lanes"].values():
        assert lane["ok"] is True
        assert [s["index"] for s in lane["steps"]] == [0, 1]
        assert all(isinstance(s["duration_s"], float) for s in lane["steps"])
        assert lane["total_s"] >= 0
        assert lane["session_id"].startswith(body["session_id"])


async def test_a_failure_is_reported_in_the_response_not_as_an_http_error() -> None:
    client, _ = make(FakeBackend(fail_steps=frozenset({0})))
    async with client:
        res = await post(client, {"steps": ["a = 1", "b = 2"]})

    steps = res.json()["lanes"]["cold"]["steps"]
    assert res.status_code == 200
    assert [s["ok"] for s in steps] == [False, True]  # the next step ran
    assert steps[0]["error_type"] == "RemoteExecutionError"


async def test_the_readie_options_are_passed_through_to_every_step() -> None:
    client, backend = make()
    async with client:
        await post(
            client,
            {
                "steps": ["a = 1", "b = 2"],
                "memory": "8Gi",
                "timeout_s": 300,
                "total_timeout_s": 600,
                "gpu": True,
                "gpu_memory": "4Gi",
                "packages": ["lightgbm==4.*", "numpy"],
                "continue_on_error": False,
            },
        )
    options = {call.options for call in backend.steps}
    assert len(options) == 1
    (only,) = options
    assert (only.memory, only.max_memory, only.timeout_s, only.gpu) == ("8Gi", "8Gi", 300.0, True)
    assert only.gpu_memory == "4Gi"
    assert only.total_timeout_s == 600.0
    assert only.packages == ("lightgbm==4.*", "numpy")
    assert only.continue_on_error is False


async def test_without_options_the_playgrounds_own_defaults_apply() -> None:
    client, backend = make(timeout_s=45.0)
    async with client:
        await post(client, {"steps": ["a = 1"]})
    (options,) = {call.options for call in backend.steps}
    assert (options.memory, options.max_memory, options.timeout_s) == ("1Gi", "1Gi", 45.0)
    assert options.continue_on_error is True


async def test_an_explicit_session_id_names_both_lanes_sessions() -> None:
    client, backend = make()
    async with client:
        res = await post(client, {"steps": ["a = 1"], "session": "abc"})
    assert res.json()["session_id"] == "abc"
    assert {c.session_id for c in backend.steps} == {"abc-optimized", "abc-cold"}


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"steps": []}, 422),
        ({"steps": ["  "]}, 422),
        ({"steps": ["a = 1"], "timeout_s": 0}, 422),
        ({"steps": ["a = 1"], "timeout_s": 99999}, 422),
        ({"steps": ["a = 1"], "total_timeout_s": 0}, 422),
        ({"steps": ["a = 1"], "total_timeout_s": 99999}, 422),
        ({"steps": ["a = 1"], "memory": "lots"}, 422),
        ({"steps": ["a = 1"], "memory": "128Gi"}, 422),
        ({"steps": ["a = 1"], "memory": "2Gi", "max_memory": "1Gi"}, 422),
        ({"steps": ["a = 1"], "packages": ["not a requirement!!"]}, 422),
        ({"steps": "a = 1"}, 422),
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
    assert backend.steps == []


async def test_too_many_steps_and_too_much_code_are_refused() -> None:
    client, _ = make(eval_max_steps=2, eval_max_code_bytes=10)
    async with client:
        many = await post(client, {"steps": ["a", "b", "c"]})
        large = await post(client, {"steps": ["x" * 11]})
    assert many.status_code == 422
    assert large.status_code == 413


async def test_the_limits_are_larger_than_the_public_playgrounds() -> None:
    # The public /run limit is 16 KiB; an evaluation sends whole notebooks.
    client, _ = make()
    async with client:
        res = await post(client, {"steps": ["x = 1\n" * 10_000]})
    assert res.status_code == 200


async def test_a_second_evaluation_while_one_runs_is_refused_not_queued() -> None:
    client, backend = make(FakeBackend(hang=True))
    async with client:
        first = asyncio.create_task(post(client, {"steps": ["a = 1"]}))
        await asyncio.sleep(0.05)
        second = await post(client, {"steps": ["a = 1"]})
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert second.status_code == 503
    assert backend.cancelled >= 1


async def test_the_public_run_route_is_unchanged_and_needs_no_token() -> None:
    client, _ = make()
    async with client:
        res = await client.post("/run", json={"code": "print(1)"})
    assert res.status_code == 200
    assert res.text.count("event: done") == 2
