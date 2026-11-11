import asyncio

import pytest

from readie_playground.runner import ExecutionOptions, Runner
from tests.fakes import FakeBackend


@pytest.mark.parametrize("cold", [False, True])
async def test_each_request_executes_once_with_the_selected_options(cold: bool) -> None:
    backend = FakeBackend(output="hello\n")
    options = ExecutionOptions(remote={"disable_optimized_execution": cold, "memory": "8Gi"})
    events = [event async for event in Runner(backend, 1000).stream("print(1)", options)]
    assert len(backend.calls) == 1
    assert backend.calls[0].options == options
    assert [event.kind for event in events] == ["log", "done"]
    assert events[-1].ok


async def test_output_is_truncated_and_flagged() -> None:
    events = [event async for event in Runner(FakeBackend(output="x" * 50), 5).stream("x")]
    assert events[0].text == "xxxxx"
    assert events[-1].truncated


@pytest.mark.parametrize(
    ("backend", "error"),
    [
        (FakeBackend(fail_execution=True), "ValueError"),
        (FakeBackend(timeout_execution=True), "RemoteTimeoutError"),
        (FakeBackend(crash_execution=True), "internal error"),
    ],
)
async def test_failures_end_the_stream_with_an_error(backend: FakeBackend, error: str) -> None:
    events = [event async for event in Runner(backend, 1000).stream("x")]
    assert not events[-1].ok
    assert error in events[-1].error


async def test_closing_the_stream_cancels_the_backend() -> None:
    backend = FakeBackend(hang=True)
    stream = Runner(backend, 1000).stream("x")
    task = asyncio.ensure_future(stream.__anext__())
    await backend.started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await stream.aclose()
    assert backend.cancelled == 1


async def test_invalid_python_fails_without_calling_readie() -> None:
    backend = FakeBackend()
    events = [event async for event in Runner(backend, 1000).stream("x = = 1")]
    assert backend.calls == []
    assert len(events) == 1
    assert not events[0].ok
    assert "SyntaxError" in events[0].error


async def test_session_requests_reuse_the_namespace_and_other_requests_do_not() -> None:
    backend = FakeBackend()
    runner = Runner(backend, 1000)
    for options in [ExecutionOptions(remote={"session": "mine"})] * 2 + [ExecutionOptions()] * 2:
        _ = [event async for event in runner.stream("x = 1", options)]
    keys = [call.key for call in backend.calls]
    assert keys[:2] == ["mine", "mine"]
    assert len(set(keys[2:])) == 2
    assert "mine" not in keys[2:]
