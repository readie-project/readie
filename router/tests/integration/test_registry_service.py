"""The worker-facing registry, over a real channel."""

from __future__ import annotations

import grpc
import pytest

from readie_router.proto import registry_pb2
from tests.integration.conftest import Harness


async def test_a_worker_registers_itself(harness: Harness) -> None:
    response = await harness.registry.PostWorkerStatus(
        registry_pb2.WorkerStatus(
            worker_id="w1", worker_uri="w1:50052", status=registry_pb2.STATUS_READY
        )
    )

    assert response.updated
    worker = harness.app.state.worker("w1")
    assert worker is not None
    assert worker.worker_uri == "w1:50052"
    assert worker.is_selectable


async def test_worker_utilization_no_longer_crashes(harness: Harness) -> None:
    """The old handler read request.container_id, which this message lacks.

    Every call raised AttributeError, and the worker's load stayed zero
    forever.
    """
    await harness.register_worker("w1")

    response = await harness.registry.PostWorkerUtilization(
        registry_pb2.WorkerUtilization(
            worker_id="w1", cpu_util=40, cpu_total=400, gpu_util=0, gpu_total=0
        )
    )

    assert response.updated
    worker = harness.app.state.worker("w1")
    assert worker is not None
    assert worker.cpu_util == 40
    assert worker.cpu_total == 400


async def test_executor_utilization_lands_on_the_container(harness: Harness) -> None:
    await harness.register_worker("w1")

    await harness.registry.PostExecutorUtilization(
        registry_pb2.ExecutorUtilization(
            worker_id="w1",
            container_id="c1",
            cpu_util=10,
            cpu_total=100,
            gpu_util=0,
            gpu_total=0,
        )
    )

    worker = harness.app.state.worker("w1")
    assert worker is not None
    assert worker.executors["c1"].cpu_util == 10


async def test_a_new_executor_record_has_every_field(harness: Harness) -> None:
    """A missing comma once produced a key called 'checkpoint_idcpu_util'.

    Both checkpoint_id and cpu_util were absent, so reading either raised
    KeyError until the first utilisation report happened to add them.
    """
    await harness.register_worker("w1")
    await harness.registry.PostExecutorStatus(
        registry_pb2.ExecutorStatus(
            worker_id="w1",
            container_id="c1",
            session_id="s1",
            request_id="r1",
            status=registry_pb2.STATUS_BUSY,
        )
    )

    worker = harness.app.state.worker("w1")
    assert worker is not None
    executor = worker.executors["c1"]
    assert executor.checkpoint_id == ""
    assert executor.cpu_util == 0
    assert executor.status == registry_pb2.STATUS_BUSY


async def test_a_removed_worker_is_evicted(harness: Harness) -> None:
    """STATUS_REMOVED was stored and acted on nowhere; state grew forever."""
    await harness.register_worker("w1")
    assert harness.app.state.worker("w1") is not None

    await harness.registry.PostWorkerStatus(
        registry_pb2.WorkerStatus(
            worker_id="w1", worker_uri="w1:50052", status=registry_pb2.STATUS_REMOVED
        )
    )

    assert harness.app.state.worker("w1") is None


async def test_a_removed_executor_unpins_its_session(harness: Harness) -> None:
    await harness.register_worker("w1")
    await harness.registry.PostExecutorStatus(
        registry_pb2.ExecutorStatus(
            worker_id="w1",
            container_id="c1",
            session_id="s1",
            request_id="r1",
            status=registry_pb2.STATUS_REMOVED,
        )
    )

    worker = harness.app.state.worker("w1")
    assert worker is not None
    assert "c1" not in worker.executors


@pytest.mark.parametrize(
    ("call", "request_message"),
    [
        ("PostWorkerStatus", registry_pb2.WorkerStatus(worker_id="")),
        ("PostWorkerUtilization", registry_pb2.WorkerUtilization(worker_id="")),
        ("PostExecutorStatus", registry_pb2.ExecutorStatus(worker_id="w1", container_id="")),
        (
            "PostExecutorUtilization",
            registry_pb2.ExecutorUtilization(worker_id="w1", container_id=""),
        ),
    ],
)
async def test_malformed_reports_are_rejected(
    harness: Harness, call: str, request_message: object
) -> None:
    """The old handlers accepted anything and answered updated=True."""
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await getattr(harness.registry, call)(request_message)

    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
