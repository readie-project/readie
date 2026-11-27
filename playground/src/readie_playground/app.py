"""The public HTTP API: single executions over SSE at ``/run``, and MCP at ``/mcp``."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from readie import Settings

from readie_playground.config import PlaygroundSettings
from readie_playground.execution import Admission, ReadieExecutionRequest, Rejected
from readie_playground.limits import RateLimiter
from readie_playground.mcp_server import create_mcp_server, mcp_routes
from readie_playground.runner import Event, ReadieBackend, Runner

__all__ = ["ReadieExecutionRequest", "create_app"]


def _frame(event: Event) -> str:
    if event.kind == "log":
        data: dict[str, object] = {"text": event.text}
    else:
        data = {
            "ok": event.ok,
            "duration_s": event.duration_s,
            "error": event.error,
            "truncated": event.truncated,
        }
    return f"event: {event.kind}\ndata: {json.dumps(data)}\n\n"


def _client_settings(settings: PlaygroundSettings) -> Settings:
    """Use the Readie client's own defaults unless the settings override them."""
    if settings.router_uri:
        return Settings(router_uri=settings.router_uri, tls=settings.router_tls)
    return Settings(tls=settings.router_tls)


def create_app(settings: PlaygroundSettings, runner: Runner | None = None) -> FastAPI:
    """Build the app. Tests pass a ``Runner`` over a fake backend."""
    if runner is None:
        backend = ReadieBackend(
            _client_settings(settings),
            Path(settings.workdir),
        )
        runner = Runner(backend, settings.max_output_chars)

    # One limiter for both front ends: a client gets the same budget over /run and /mcp.
    admission = Admission(
        settings, RateLimiter(settings.rate_limit_requests, settings.rate_limit_window_s)
    )
    mcp = create_mcp_server(admission, runner)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    app = FastAPI(
        title="Readie playground",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["POST"],
        allow_headers=["content-type"],
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/run")
    async def run(body: ReadieExecutionRequest, request: Request) -> StreamingResponse:
        try:
            options = admission.admit(body, request)
        except Rejected as exc:
            raise HTTPException(exc.status, exc.detail) from exc

        async def events() -> AsyncIterator[str]:
            async for event in runner.stream(body.code, options):
                yield _frame(event)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    app.router.routes.extend(mcp_routes(mcp))
    return app
