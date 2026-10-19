"""Packages and pinned checkpoints, from the client config to the worker request."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import grpc
import pytest
from structlog.testing import capture_logs

from readie_router.config import Settings
from readie_router.proto import proxy_pb2
from readie_router.scheduling.catalogue import CATALOGUE_VERSION
from tests.integration.conftest import Harness
from tests.integration.test_proxy_service import chunk, execute


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    """A router that loads a cpu catalogue listing one checkpoint and numpy."""
    (tmp_path / "cpu.json").write_text(
        json.dumps(
            {
                "version": CATALOGUE_VERSION,
                "flavor": "cpu",
                "alpha": 0.005,
                # scipy is in the image with no recorded version.
                "distributions": {"numpy": "2.0.2", "scipy": ""},
                "items": {},
                "checkpoints": [{"id": "c-data", "items": [], "size_mb": 0.0}],
            }
        )
    )
    return settings.model_copy(update={"catalogue_dir": str(tmp_path)})


def header(**config: Any) -> proxy_pb2.ClientExecutionRequest:
    return proxy_pb2.ClientExecutionRequest(
        request_id="req-1", session_id="", config=proxy_pb2.ExecutionConfig(**config)
    )


async def test_packages_the_image_lacks_reach_the_worker_deduped_and_sorted(
    harness: Harness,
) -> None:
    await harness.register_worker()

    await execute(harness, header(packages=["Requests", "flask", "requests==1.0"]), chunk(b"x"))

    assert list(harness.worker.calls[0].header.packages) == ["flask", "Requests"]


async def test_a_package_the_image_has_is_dropped_and_the_client_is_told(
    harness: Harness,
) -> None:
    await harness.register_worker()

    responses = await execute(harness, header(packages=["numpy==2.0", "flask"]), chunk(b"x"))

    assert list(harness.worker.calls[0].header.packages) == ["flask"]
    logs = [r.logs for r in responses if r.HasField("logs")]
    assert "ignored numpy==2.0: already provided by the base image (version 2.0.2)\n" in logs
    # Pinned: the note once went out with success unset (False), and the client
    # treats any such message as a failed execution.
    assert all(r.success for r in responses)


async def test_the_note_has_no_version_when_the_catalogue_recorded_none(harness: Harness) -> None:
    await harness.register_worker()

    responses = await execute(harness, header(packages=["scipy>=1.0"]), chunk(b"x"))

    logs = [r.logs for r in responses if r.HasField("logs")]
    assert "ignored scipy>=1.0: already provided by the base image\n" in logs


async def test_the_router_logs_the_packages_it_did_not_install(harness: Harness) -> None:
    await harness.register_worker()

    with capture_logs() as logs:
        await execute(harness, header(packages=["numpy==2.0", "flask"]), chunk(b"x"))

    notes = [e for e in logs if e["event"].startswith("packages already in the base image")]
    assert len(notes) == 1
    assert notes[0]["ignored_packages"] == ["numpy==2.0"]
    assert notes[0]["installed_versions"] == {"numpy==2.0": "2.0.2"}
    assert notes[0]["request_id"] == "req-1"


async def test_nothing_is_logged_when_every_package_is_installed(harness: Harness) -> None:
    await harness.register_worker()

    with capture_logs() as logs:
        await execute(harness, header(packages=["flask"]), chunk(b"x"))

    assert not [e for e in logs if "base image" in e["event"]]


async def test_an_invalid_package_requirement_is_rejected(harness: Harness) -> None:
    await harness.register_worker()

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await execute(
            harness, header(packages=["--index-url=https://example.invalid"]), chunk(b"x")
        )

    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_a_pinned_checkpoint_reaches_the_worker(harness: Harness) -> None:
    await harness.register_worker()

    await execute(harness, header(checkpoint_id="c-data"), chunk(b"x"))

    assert harness.worker.calls[0].header.checkpoint_id == "c-data"


async def test_an_unknown_pinned_checkpoint_is_invalid_argument_not_not_found(
    harness: Harness,
) -> None:
    # The client maps NOT_FOUND to an expired session, so a pin that names no
    # checkpoint must not use it.
    await harness.register_worker()

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await execute(harness, header(checkpoint_id="nope"), chunk(b"x"))

    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_a_pin_together_with_disable_optimized_execution_is_rejected(
    harness: Harness,
) -> None:
    await harness.register_worker()

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await execute(
            harness, header(checkpoint_id="c-data", disable_optimized_execution=True), chunk(b"x")
        )

    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
