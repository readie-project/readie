"""MCP over streamable HTTP at ``/mcp``: the same executions as ``/run``, as agent tools.

The server is stateless and answers in JSON, so any replica can take any request
and nothing is kept between calls. Readie sessions are a tool argument, as they are
a field of ``/run``. Every call to ``run_python`` passes the same ``Admission`` as a
``/run`` request and runs through the same ``Runner``.
"""

from __future__ import annotations

import secrets
from importlib.metadata import version
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.routing import BaseRoute

from readie_playground.execution import Admission, ReadieExecutionRequest, Rejected
from readie_playground.runner import Runner

MCP_PATH = "/mcp"

INSTRUCTIONS = (
    "Readie runs Python in gVisor sandboxes restored from checkpoints that already have "
    "common libraries imported. run_python executes a script and returns what it prints; "
    "print() every value you need. The code runs on the Readie service, not on the user's "
    "machine: local files and modules are not available, and secrets must not be sent. "
    "Use new_session for an id when several calls must share Python state, and send "
    "those calls one at a time."
)

_SIZE = "A size: bytes, or a whole number with a unit such as 512Mi, 4Gi or 2G."


def new_session_id() -> str:
    """An unpredictable session id in the format the Python client uses."""
    return f"sess-{secrets.token_hex(16)}"


def format_result(output: str, *, ok: bool, duration_s: float, error: str, truncated: bool) -> str:
    """The text an agent reads for one finished execution."""
    parts = [output or "(no output)"]
    if not parts[0].endswith("\n"):
        parts.append("\n")
    parts.append("---\n")
    if ok:
        parts.append(f"Succeeded in {duration_s:.3f} s.\n")
        if not output:
            parts.append("The code printed nothing. Only printed output is returned.\n")
    else:
        parts.append(f"Failed after {duration_s:.3f} s.\n")
        if error:
            parts.append(f"Error: {error}\n")
    if truncated:
        parts.append("The output reached its size limit and was cut off.\n")
    return "".join(parts)


def create_mcp_server(admission: Admission, runner: Runner) -> MCPServer:
    """Build the MCP server whose tools run code through ``admission`` and ``runner``."""
    server = MCPServer(
        name="readie",
        title="Readie",
        instructions=INSTRUCTIONS,
        website_url="https://readie.org",
        version=version("readie-playground"),
    )

    # The parameters are the fields of ReadieExecutionRequest, one for one; a test
    # compares them. Each is passed on untouched, so /run and run_python accept the
    # same values and reject the same ones.
    @server.tool(
        name="run_python",
        title="Run Python on Readie",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
        structured_output=False,
    )
    async def run_python(
        ctx: Context,
        *,
        code: Annotated[
            str,
            Field(description="Python source, run as a script. Only printed output comes back."),
        ],
        # The Readie call's own deadline, passed on to @remote, not an asyncio timeout.
        timeout: Annotated[  # noqa: ASYNC109
            float | None, Field(description="Deadline for the whole call, in seconds.")
        ] = None,
        session: Annotated[
            str | None,
            Field(
                description="Session id from new_session. Calls with the same id share "
                "Python state while the session lives."
            ),
        ] = None,
        gpu: Annotated[bool, Field(description="Run on a GPU worker.")] = False,
        memory: Annotated[str | int | None, Field(description=f"Starting memory. {_SIZE}")] = None,
        max_memory: Annotated[
            str | int | None, Field(description=f"Memory ceiling. {_SIZE}")
        ] = None,
        gpu_memory: Annotated[
            str | int | None, Field(description=f"GPU memory to reserve; implies gpu. {_SIZE}")
        ] = None,
        max_gpu_memory: Annotated[
            str | int | None, Field(description=f"GPU memory ceiling. {_SIZE}")
        ] = None,
        packages: Annotated[
            list[str] | None,
            Field(
                description="PEP 508 requirements to install before the code runs, such as "
                "httpx==0.27.2. Packages already in the sandbox are skipped."
            ),
        ] = None,
        disable_optimized_execution: Annotated[
            bool, Field(description="Start cold instead of restoring a checkpoint.")
        ] = False,
        checkpoint_id: Annotated[
            str | None, Field(description="Checkpoint to restore. Readie chooses one when unset.")
        ] = None,
    ) -> str:
        """Run a Python script on Readie and return what it prints."""
        body = ReadieExecutionRequest(
            code=code,
            timeout=timeout,
            session=session,
            gpu=gpu,
            memory=memory,
            max_memory=max_memory,
            gpu_memory=gpu_memory,
            max_gpu_memory=max_gpu_memory,
            packages=packages,
            disable_optimized_execution=disable_optimized_execution,
            checkpoint_id=checkpoint_id,
        )
        request = ctx.request_context.request
        if not isinstance(request, Request):
            raise ToolError("run_python is only served over HTTP.")
        try:
            options = admission.admit(body, request)
        except Rejected as exc:
            raise ToolError(exc.detail) from exc
        chunks: list[str] = []
        async for event in runner.stream(body.code, options):
            if event.kind == "log":
                chunks.append(event.text)
                continue
            text = format_result(
                "".join(chunks),
                ok=event.ok,
                duration_s=event.duration_s,
                error=event.error,
                truncated=event.truncated,
            )
            if not event.ok:
                raise ToolError(text)
            return text
        raise ToolError("The execution ended without a result.")

    @server.tool(
        name="new_session",
        title="New Readie session id",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        structured_output=False,
    )
    def new_session() -> str:
        """Return a new unpredictable session id for run_python.

        Anyone who knows a session id can run code in it, so use a fresh one.
        """
        return new_session_id()

    return server


def mcp_routes(server: MCPServer) -> list[BaseRoute]:
    """The routes that serve ``server`` at ``/mcp``, to add to the app.

    Stateless with JSON responses: no MCP session is held, so replicas need no
    affinity. Host and Origin checks are off because nginx only forwards the public
    hostname, and the service is not reachable otherwise.
    """
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    routes: list[Any] = list(app.routes)
    return routes
