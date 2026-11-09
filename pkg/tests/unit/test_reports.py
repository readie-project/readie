"""Core timing, cancellation, and bounded-delivery guarantees."""

from __future__ import annotations

import asyncio
import threading

import pytest

from readie._proto import proxy_pb2 as pb
from readie._reports import ClientReporter, get_report_token
from readie.config import Settings
from readie.protocol import CallRef


class Clock:
    wall = 1000
    mono = 100

    def wall_ns(self):
        return self.wall

    def monotonic_ns(self):
        return self.mono


class Sender:
    def __init__(self):
        self.reports: list[pb.SubmitClientReportRequest] = []
        self.closed = threading.Event()
        self.sent = threading.Event()
        self.block = False

    def send(self, report):
        self.reports.append(report)
        self.sent.set()
        if self.block:
            self.closed.wait(3)

    def close(self):
        self.closed.set()


@pytest.fixture
def rig():
    clock, sender = Clock(), Sender()
    reporter = ClientReporter(Settings(client_reports=True), clock=clock, sender=sender)
    yield reporter, clock, sender
    reporter.close()


def finish_call(reporter: ClientReporter, failure: BaseException | None = None):
    with reporter.time_call() as call_info:
        ref = CallRef("request", "")
        reporter.register_call(ref, call_info)
        reporter.register_report_token(ref, "a" * 43)
        if failure is not None:
            raise failure


def test_overlapping_calls_keep_their_tokens_and_use_monotonic_duration(rig):
    reporter, clock, sender = rig
    with reporter.time_call() as first, reporter.time_call() as second:
        reporter.register_call(CallRef("first", ""), first)
        reporter.register_call(CallRef("second", ""), second)
        reporter.register_report_token(CallRef("second", ""), "b" * 43)
        reporter.register_report_token(CallRef("first", ""), "a" * 43)
        clock.wall, clock.mono = 900, 150
    reporter.close()
    assert {report.token for report in sender.reports} == {"a" * 43, "b" * 43}
    for report in sender.reports:
        assert (report.start_time_unix_ns, report.end_time_unix_ns) == (1000, 900)
        assert report.duration_ns == 50
    assert not reporter._requests


def test_cancellation_is_reported_without_replacing_the_exception(rig):
    reporter, _clock, sender = rig
    failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as raised:
        finish_call(reporter, failure)
    assert raised.value is failure
    reporter.close()
    assert sender.reports[0].outcome == pb.CLIENT_REPORT_OUTCOME_CANCELLED


def test_queue_overflow_and_shutdown_do_not_wait_for_slow_delivery(monkeypatch):
    monkeypatch.setattr("readie._reports.QUEUE_CAPACITY", 2)
    monkeypatch.setattr("readie._reports.CLOSE_TIMEOUT", 0.02)
    sender = Sender()
    sender.block = True
    reporter = ClientReporter(Settings(client_reports=True), sender=sender)
    try:
        finish_call(reporter)
        assert sender.sent.wait(1)
        for _ in range(4):
            finish_call(reporter)
        assert reporter._completion_queue.qsize() == 2
    finally:
        reporter.close()
    assert sender.closed.is_set()
    assert reporter._thread is not None
    reporter._thread.join(timeout=1)
    assert not reporter._thread.is_alive()
    assert reporter._completion_queue.empty()


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [(None, None), (["malformed"], None), ([("readie-client-report-token", "a" * 43)], "a" * 43)],
)
def test_report_token_extraction_handles_optional_metadata(metadata, expected):
    assert get_report_token(metadata) == expected
