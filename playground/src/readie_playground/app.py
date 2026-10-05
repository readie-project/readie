"""The FastAPI app: ``POST /run`` streams both lanes as server-sent events."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from readie import Settings

from readie_playground.config import PlaygroundSettings
from readie_playground.limits import RateLimiter, RunSlots
from readie_playground.runner import Event, ReadieBackend, Runner


class RunRequest(BaseModel):
    """Body of ``POST /run``. Only source text; the service wraps it."""

    code: str


def _frame(event: Event) -> str:
    if event.kind == "log":
        data: dict[str, object] = {"lane": event.lane.value, "text": event.text}
    else:
        data = {
            "lane": event.lane.value,
            "ok": event.ok,
            "duration_s": event.duration_s,
            "error": event.error,
            "truncated": event.truncated,
        }
    return f"event: {event.kind}\ndata: {json.dumps(data)}\n\n"


def _client_key(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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
            settings.timeout_s,
            Path(settings.workdir),
        )
        runner = Runner(backend, settings.max_output_chars)
    limiter = RateLimiter(settings.rate_limit_requests, settings.rate_limit_window_s)
    slots = RunSlots(settings.max_concurrent_runs)

    app = FastAPI(title="Readie playground", docs_url=None, redoc_url=None, openapi_url=None)
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
    async def run(body: RunRequest, request: Request) -> StreamingResponse:
        if len(body.code.encode()) > settings.max_code_bytes:
            raise HTTPException(413, "The code is too large.")
        if not body.code.strip():
            raise HTTPException(422, "Enter some code to run.")
        if not limiter.allow(_client_key(request, settings.trust_proxy)):
            raise HTTPException(429, "Too many runs. Wait a moment and try again.")
        if not slots.try_acquire():
            raise HTTPException(503, "The playground is busy. Try again shortly.")

        async def events() -> AsyncIterator[str]:
            try:
                async for event in runner.stream(body.code):
                    yield _frame(event)
            finally:
                slots.release()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app
