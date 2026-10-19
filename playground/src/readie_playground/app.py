"""The FastAPI app.

``POST /run`` streams both lanes of one script as server-sent events; it is the
public playground. ``POST /eval/run`` is for evaluations: it takes one piece of code
and the arguments of the Readie remote function, passes them to it as given, runs
the code once, and returns its timing as JSON. It has no token and is always served;
the nginx configs answer 404 for ``/playground/eval/``, so it is reachable only on the
playground's own network.
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
    """Body of ``POST /eval/run``: the code, and the arguments of ``readie.remote``.

    Apart from ``code``, every field is an argument of ``@remote`` under its own
    name, and goes to it untouched. ``session`` is the
    session id (``remote`` takes a ``Session`` made from it). A field left out
    keeps ``remote``'s own default. Unknown fields are refused.
    """

    model_config = ConfigDict(extra="forbid")

    code: str

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
    the request up front instead of failing the call.
    """
    arguments = body.model_dump(exclude={"code"})
    if not arguments["session"]:
        # An empty id means no session. Passed on, it would become ``Session("")``, which
        # makes up a random id and leaves a warm container nobody can reach again.
        arguments["session"] = None
    try:
        remote(**arguments)
    except ReadieError as exc:
        raise HTTPException(422, str(exc)) from exc
    return EvalOptions(remote=arguments)


def _eval_payload(result: EvalResult) -> dict[str, object]:
    return {
        "session_id": result.session_id,
        "ok": result.ok,
        "duration_s": result.duration_s,
        "error": result.error,
        "error_type": result.error_type,
        "output": result.output,
        "truncated": result.truncated,
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
        if not body.code.strip():
            raise HTTPException(422, "Send some code to run.")
        if len(body.code.encode()) > settings.eval_max_code_bytes:
            raise HTTPException(413, "The code is too large.")
        options = _eval_options(body)
        result = await runner.run_eval(body.code, options)
        return _eval_payload(result)

    return app
