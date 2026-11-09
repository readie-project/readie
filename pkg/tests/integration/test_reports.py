"""Built-in transports report privately without affecting execution."""

from __future__ import annotations

import asyncio

import grpc
import pytest
import pytest_asyncio

from readie import Client, RemoteExecutionError, Settings
from readie._proto import proxy_pb2 as pb
from tests.fakes.router import RouterHarness


def add(a, b):
    return a + b


async def wait_reports(harness: RouterHarness, count: int = 1):
    async with asyncio.timeout(3), harness.router.report_received:
        await harness.router.report_received.wait_for(lambda: len(harness.router.reports) >= count)


@pytest_asyncio.fixture
async def reporting_client(harness, monkeypatch):
    monkeypatch.setenv("READIE_AUTH_TOKEN", "secret")
    harness.router.offer_reports = True
    async with Client(Settings(router_uri=harness.uri, tls=False, client_reports=True)) as client:
        yield client


@pytest.mark.parametrize("blocking", [False, True])
async def test_both_transports_report_completed_calls(harness, reporting_client, blocking):
    if blocking:
        assert await asyncio.to_thread(reporting_client.call, add, (2, 3)) == 5
    else:
        assert await reporting_client.acall(add, (2, 3)) == 5
    await wait_reports(harness)
    report = harness.router.reports[0]
    assert report.token in harness.router.report_tokens.values()
    assert report.duration_ns > 0
    assert report.outcome == pb.CLIENT_REPORT_OUTCOME_SUCCESS


async def test_remote_errors_are_reported_without_private_exception_details(
    harness, reporting_client
):
    harness.router.envelope = {"ok": False, "exc_type": "PrivateType", "message": "secret"}
    with pytest.raises(RemoteExecutionError, match="secret"):
        await reporting_client.acall(add, (1, 2))
    await wait_reports(harness)
    report = harness.router.reports[0]
    assert report.outcome == pb.CLIENT_REPORT_OUTCOME_ERROR
    assert report.error == pb.CLIENT_REPORT_ERROR_REMOTE_EXECUTION
    assert b"secret" not in report.SerializeToString()
    assert b"PrivateType" not in report.SerializeToString()


async def test_slow_uploads_do_not_delay_results(harness, reporting_client):
    harness.router.report_delay = 5
    assert await asyncio.wait_for(reporting_client.acall(add, (1, 2)), 1) == 3
    assert not harness.router.reports


@pytest.mark.parametrize(
    ("status", "attempts"),
    [(grpc.StatusCode.UNAVAILABLE, 2), (grpc.StatusCode.PERMISSION_DENIED, 1)],
)
async def test_uploads_authenticate_and_retry_only_transient_errors(
    harness, reporting_client, status, attempts
):
    harness.router.report_abort = status
    assert await reporting_client.acall(add, (1, 2)) == 3
    await wait_reports(harness, attempts)
    await reporting_client.aclose()
    assert len(harness.router.reports) == attempts
    assert harness.router.reports[0] == harness.router.reports[-1]
    assert ("authorization", "Bearer secret") in harness.router.report_metadata


@pytest.mark.parametrize(("enabled", "supported"), [(False, True), (True, False)])
async def test_opt_out_and_older_services_skip_uploads(harness, enabled, supported):
    harness.router.offer_reports = supported
    async with Client(
        Settings(router_uri=harness.uri, tls=False, client_reports=enabled)
    ) as client:
        assert await client.acall(add, (1, 2)) == 3
    assert harness.router.headers[0].config.client_reports == enabled
    assert not harness.router.reports
