import asyncio

from readie_playground.runner import Event, Lane, Runner
from tests.fakes import FakeBackend


async def collect(runner: Runner, code: str) -> list[Event]:
    return [e async for e in runner.stream(code)]


async def test_runs_both_lanes_with_the_code_untouched() -> None:
    backend = FakeBackend()
    events = await collect(Runner(backend, 1000), "print(1)  # raw")
    assert sorted(backend.calls, key=lambda c: c[1]) == [
        ("print(1)  # raw", False),
        ("print(1)  # raw", True),
    ]
    done = {e.lane: e for e in events if e.kind == "done"}
    assert set(done) == {Lane.OPTIMIZED, Lane.COLD}
    assert all(e.ok for e in done.values())


async def test_logs_are_tagged_by_lane() -> None:
    events = await collect(Runner(FakeBackend(), 1000), "x")
    logs = {e.lane: e.text for e in events if e.kind == "log"}
    assert logs == {Lane.OPTIMIZED: "fast output\n", Lane.COLD: "cold output\n"}


async def test_one_lane_failing_does_not_fail_the_other() -> None:
    events = await collect(Runner(FakeBackend(fail_cold=True), 1000), "x")
    done = {e.lane: e for e in events if e.kind == "done"}
    assert done[Lane.OPTIMIZED].ok
    assert not done[Lane.COLD].ok
    assert "ValueError" in done[Lane.COLD].error


async def test_output_is_truncated_and_flagged() -> None:
    events = await collect(Runner(FakeBackend(), 5), "x")
    assert all(len(e.text) <= 5 for e in events if e.kind == "log")
    assert all(e.truncated for e in events if e.kind == "done")


async def test_closing_the_stream_cancels_both_runs() -> None:
    backend = FakeBackend(hang=True)
    stream = Runner(backend, 1000).stream("x")
    task = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await stream.aclose()
    assert backend.cancelled == 2


async def test_invalid_python_fails_both_lanes_without_calling_readie() -> None:
    backend = FakeBackend()
    events = await collect(Runner(backend, 1000), "x = = 1")
    assert backend.calls == []
    assert [e.ok for e in events] == [False, False]
    assert all("SyntaxError" in e.error for e in events)
