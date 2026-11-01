"""The public comparison path uses SDK resource defaults."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from readie import Settings
from readie.budget import Budget, ResourceKind

from readie_playground.app import create_app
from readie_playground.config import PlaygroundSettings
from readie_playground.runner import ReadieBackend, Runner


async def test_comparison_does_not_override_the_sdk_memory_budget(tmp_path: Path) -> None:
    backend = ReadieBackend(Settings(), 30, tmp_path)
    with (
        patch("readie_playground.runner.Client"),
        patch("readie_playground.runner.RemoteFunction") as remote_function,
    ):
        remote_function.return_value.aio = AsyncMock()
        await backend.execute("print(1)", cold=False, on_log=lambda _: None)

    arguments = remote_function.call_args.kwargs
    assert "budgets" not in arguments
    assert "session" not in arguments
    assert arguments["timeout"] == 30
    assert arguments["disable_optimized_execution"] is False


async def test_http_options_reach_the_real_sdk_call(tmp_path: Path) -> None:
    backend = ReadieBackend(Settings(), 30, tmp_path)
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
