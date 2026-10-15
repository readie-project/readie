"""The FastAPI app.

``POST /run`` streams both lanes of one script as server-sent events; it is the
public playground. ``POST /eval/run`` is for evaluations: it takes a list of steps
and the arguments of the Readie remote function, passes them to it as given, runs
the steps once, and returns every step's timing as JSON. It exists only when
It has no token: it is always served, and the nginx configs answer 404 for
``/playground/eval/`` so that it is reachable only on the playground's own network.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from readie import ReadieError, Settings, remote

from readie_playground.config import PlaygroundSettings
from readie_playground.limits import RateLimiter, RunSlots
from readie_playground.runner import EvalOptions, EvalResult, Event, ReadieBackend, Runner


class RunRequest(BaseModel):
    """Body of ``POST /run``. Only source text; the service wraps it."""

    code: str


class EvalRequest(BaseModel):
    """Body of ``POST /eval/run``: the steps, and the arguments of ``readie.remote``.

    Apart from ``steps`` and ``continue_on_error``, every field is an argument of
    ``@remote`` under its own name, and goes to it untouched. ``session`` is the
    session id (``remote`` takes a ``Session`` made from it). A field left out
    keeps ``remote``'s own default. Unknown fields are refused.
    """

    model_config = ConfigDict(extra="forbid")

    steps: list[str]
    continue_on_error: bool = True

    timeout: float | None = None
    session: str | None = None
    gpu: bool = False
    memory: str | int | None = None
    max_memory: str | int | None = None
    gpu_memory: str | int | None = None
    max_gpu_memory: str | int | None = None
    packages: list[str] | None = None
    disable_optimized_execution: bool = False
    checkpoint_id: str | None = None


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


def _eval_options(body: EvalRequest) -> EvalOptions:
    """Turn a request into the options passed to ``readie.remote``.

    Nothing is changed on the way. ``remote`` itself is called once, here, only so
    that its own validation (a bad size, a bad requirement, a contradiction) rejects
    the request up front instead of failing every step.
    """
    arguments = body.model_dump(exclude={"steps", "continue_on_error"})
    try:
        remote(**arguments)
    except ReadieError as exc:
        raise HTTPException(422, str(exc)) from exc
    return EvalOptions(remote=arguments, continue_on_error=body.continue_on_error)


def _eval_payload(result: EvalResult) -> dict[str, object]:
    return {
        "session_id": result.session_id,
        "ok": result.ok,
        "total_s": result.total_s,
        "steps": [
            {
                "index": step.index,
                "ok": step.ok,
                "duration_s": step.duration_s,
                "error": step.error,
                "error_type": step.error_type,
                "output": step.output,
                "truncated": step.truncated,
            }
            for step in result.steps
        ],
    }


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

    @app.post("/eval/run")
    async def eval_run(body: EvalRequest) -> dict[str, object]:
        if not 1 <= len(body.steps) <= settings.eval_max_steps:
            raise HTTPException(422, f"Send between 1 and {settings.eval_max_steps} steps.")
        if any(not step.strip() for step in body.steps):
            raise HTTPException(422, "Every step needs some code.")
        if sum(len(step.encode()) for step in body.steps) > settings.eval_max_code_bytes:
            raise HTTPException(413, "The code is too large.")
        options = _eval_options(body)
        result = await runner.run_eval(body.steps, options)
        return _eval_payload(result)

    return app
