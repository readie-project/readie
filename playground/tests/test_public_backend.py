"""HTTP execution options reach the SDK."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from readie import Settings
from readie.budget import Budget, ResourceKind

from readie_playground.app import create_app
from readie_playground.config import PlaygroundSettings
from readie_playground.runner import ReadieBackend, Runner


async def test_http_options_reach_the_real_sdk_call(tmp_path: Path) -> None:
    backend = ReadieBackend(Settings(), tmp_path)
    app = create_app(PlaygroundSettings(), Runner(backend, 1000))
    with patch("readie_playground.runner.Client") as client_factory:
        sdk_client = client_factory.return_value.__aenter__.return_value
        sdk_client.acall = AsyncMock(return_value=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as http:
            response = await http.post(
                "/run",
                json={
                    "code": "print(42)",
                    "timeout": 300,
                    "session": "http-session",
                    "gpu": True,
                    "memory": "8Gi",
                    "max_memory": "16Gi",
                    "gpu_memory": "2Gi",
                    "max_gpu_memory": "4Gi",
                    "packages": ["numpy==2.2.0"],
                    "disable_optimized_execution": False,
                    "checkpoint_id": "selected-checkpoint",
                },
            )

    assert response.status_code == 200
    assert response.text.count("event: done") == 1
    assert '"ok": true' in response.text
    sdk_client.acall.assert_awaited_once()
    arguments = sdk_client.acall.call_args.kwargs
    assert arguments["timeout"] == 300
    assert arguments["session"].id == "http-session"
    assert arguments["gpu"] is True
    assert arguments["budgets"] == (
        Budget(ResourceKind.MEMORY, 8 * 1024**3, 16 * 1024**3),
        Budget(ResourceKind.GPU_MEMORY, 2 * 1024**3, 4 * 1024**3),
    )
    assert arguments["packages"] == ("numpy==2.2.0",)
    assert arguments["disable_optimized_execution"] is False
    assert arguments["checkpoint_id"] == "selected-checkpoint"


async def test_code_only_http_request_leaves_resources_and_deadline_to_the_sdk(
    tmp_path: Path,
) -> None:
    app = create_app(PlaygroundSettings(), Runner(ReadieBackend(Settings(), tmp_path), 1000))
    with patch("readie_playground.runner.Client") as factory:
        sdk = factory.return_value.__aenter__.return_value
        sdk.acall = AsyncMock(return_value=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as http:
            response = await http.post("/run", json={"code": "print(42)"})
    assert response.text.count("event: done") == 1
    sdk.acall.assert_awaited_once()
    arguments = sdk.acall.call_args.kwargs
    assert arguments["budgets"] == ()
    assert arguments["timeout"] is None
    assert arguments["session"] is None
