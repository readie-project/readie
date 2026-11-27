"""What a request to run code carries, and the checks it passes before it runs.

``POST /run`` and the MCP ``run_python`` tool both admit requests here, so the two
front ends share one request model, one set of limits and one rate limiter.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from readie import ReadieError, remote
from starlette.requests import Request

from readie_playground.config import PlaygroundSettings
from readie_playground.limits import RateLimiter
from readie_playground.runner import ExecutionOptions


class ReadieExecutionRequest(BaseModel):
    """Body of ``POST /run``, and the arguments of the MCP ``run_python`` tool.

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


@dataclass(frozen=True, slots=True)
class Rejected(Exception):  # noqa: N818 - reads as "the request was rejected"
    """A request that fails admission, with the HTTP status that ``/run`` answers."""

    status: int
    detail: str

    def __str__(self) -> str:
        """The detail, which is what the caller sees."""
        return self.detail


def client_key(request: Request, trust_proxy: bool) -> str:
    """The address a request is rate-limited by."""
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def execution_options(body: ReadieExecutionRequest) -> ExecutionOptions:
    """Turn a request into the options passed to ``readie.remote``.

    Nothing is changed on the way. ``remote`` itself is called once, here, only so
    that its own validation (a bad size, a bad requirement, a contradiction) rejects
    the request up front instead of failing the call.

    Raises:
        Rejected: ``remote`` refused the options.
    """
    arguments = body.model_dump(exclude={"code"})
    if not arguments["session"]:
        # An empty id means no session. Passed on, it would become ``Session("")``, which
        # makes up a random id and leaves a warm container nobody can reach again.
        arguments["session"] = None
    try:
        remote(**arguments)
    except ReadieError as exc:
        raise Rejected(422, str(exc)) from exc
    return ExecutionOptions(remote=arguments)


class Admission:
    """The checks every request passes before it runs, in the order ``/run`` applies them."""

    def __init__(self, settings: PlaygroundSettings, limiter: RateLimiter) -> None:
        self._settings = settings
        self._limiter = limiter

    def admit(self, body: ReadieExecutionRequest, request: Request) -> ExecutionOptions:
        """Check size, content, rate and options; return the options to run with.

        Raises:
            Rejected: The request fails a check.
        """
        if len(body.code.encode()) > self._settings.max_code_bytes:
            raise Rejected(413, "The code is too large.")
        if not body.code.strip():
            raise Rejected(422, "Enter some code to run.")
        if not self._limiter.allow(client_key(request, self._settings.trust_proxy)):
            raise Rejected(429, "Too many runs. Wait a moment and try again.")
        return execution_options(body)
