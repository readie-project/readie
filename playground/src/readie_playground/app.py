"""The FastAPI app.

``POST /run`` streams both lanes of one script as server-sent events; it is the
public playground. ``POST /eval/run`` is for evaluations: it takes a list of steps
and the options of the Readie remote function, runs both lanes, and returns every
step's timing as JSON. It exists only when ``PLAYGROUND_EVAL_TOKEN`` is set, needs
that token, and is not published by nginx.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from readie import ReadieError, ResourceKind, Settings
from readie.budget import build_budgets
from readie.packages import normalize_packages

from readie_playground.config import MEMORY, PlaygroundSettings
from readie_playground.limits import RateLimiter, RunSlots
from readie_playground.runner import EvalOptions, EvalOutcome, Event, ReadieBackend, Runner


class RunRequest(BaseModel):
    """Body of ``POST /run``. Only source text; the service wraps it."""

    code: str


class EvalRequest(BaseModel):
    """Body of ``POST /eval/run``: the steps to run and how to run them."""

    steps: list[str]
    session: str | None = None
    memory: str = MEMORY
    max_memory: str | None = None
    gpu: bool = False
    gpu_memory: str | None = None
    max_gpu_memory: str | None = None
    packages: list[str] = Field(default_factory=list)
    timeout_s: float | None = None
    total_timeout_s: float | None = None
    continue_on_error: bool = True


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


def _authorise(request: Request, token: str) -> None:
    """Require ``Authorization: Bearer <token>``, compared in constant time."""
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        supplied.strip().encode(), token.encode()
    ):
        raise HTTPException(401, "Missing or wrong token.", headers={"WWW-Authenticate": "Bearer"})


def _eval_options(body: EvalRequest, settings: PlaygroundSettings) -> EvalOptions:
    """Validate an evaluation request into options for the Readie remote function."""
    timeout = settings.timeout_s if body.timeout_s is None else body.timeout_s
    if not 0 < timeout <= settings.eval_max_timeout_s:
        raise HTTPException(422, f"timeout_s must be between 0 and {settings.eval_max_timeout_s}.")
    total = body.total_timeout_s
    if total is not None and not 0 < total <= settings.eval_max_timeout_s:
        raise HTTPException(
            422, f"total_timeout_s must be between 0 and {settings.eval_max_timeout_s}."
        )
    try:
        budgets = build_budgets(
            memory=body.memory,
            max_memory=body.max_memory or body.memory,
            gpu_memory=body.gpu_memory,
            max_gpu_memory=body.max_gpu_memory,
        )
        cap = build_budgets(memory=settings.eval_max_memory)[0].alloc
        packages = normalize_packages(body.packages)
    except ReadieError as exc:
        raise HTTPException(422, str(exc)) from exc
    if any(b.kind is ResourceKind.MEMORY and max(b.alloc, b.max) > cap for b in budgets):
        raise HTTPException(422, f"memory may not exceed {settings.eval_max_memory}.")
    return EvalOptions(
        memory=body.memory,
        max_memory=body.max_memory or body.memory,
        gpu=body.gpu,
        gpu_memory=body.gpu_memory,
        max_gpu_memory=body.max_gpu_memory,
        packages=packages,
        timeout_s=timeout,
        total_timeout_s=total,
        continue_on_error=body.continue_on_error,
    )


def _eval_payload(outcome: EvalOutcome) -> dict[str, object]:
    return {
        "session_id": outcome.session_id,
        "lanes": {
            lane.value: {
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
            for lane, result in outcome.lanes.items()
        },
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

    if settings.eval_token:
        eval_slots = RunSlots(settings.eval_max_concurrent_runs)

        @app.post("/eval/run")
        async def eval_run(body: EvalRequest, request: Request) -> dict[str, object]:
            _authorise(request, settings.eval_token)
            if not 1 <= len(body.steps) <= settings.eval_max_steps:
                raise HTTPException(422, f"Send between 1 and {settings.eval_max_steps} steps.")
            if any(not step.strip() for step in body.steps):
                raise HTTPException(422, "Every step needs some code.")
            if sum(len(step.encode()) for step in body.steps) > settings.eval_max_code_bytes:
                raise HTTPException(413, "The code is too large.")
            options = _eval_options(body, settings)
            if not eval_slots.try_acquire():
                raise HTTPException(503, "An evaluation is already running.")
            try:
                outcome = await runner.run_eval(body.steps, options, session=body.session)
            finally:
                eval_slots.release()
            return _eval_payload(outcome)

    return app
