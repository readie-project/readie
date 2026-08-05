"""An in-process router that behaves like the real one.

It speaks the actual ProxyService over a real aio server on 127.0.0.1:0, so the
integration tests exercise gRPC framing, the oneof, streaming and status codes
rather than a mock of them. What it does *not* do is schedule: it executes the
payload itself, which is enough to prove the client's half of the contract.
"""

from __future__ import annotations

import asyncio
import traceback

import cloudpickle
import grpc

from crfs._proto import proxy_pb2, proxy_pb2_grpc


class FakeRouter(proxy_pb2_grpc.ProxyServiceServicer):
    """A programmable ProxyService.

    Set ``abort_with`` to fail the call with a status, ``logs`` to emit executor
    output, ``swallow_result`` to reproduce the executor's user-exception path
    (success, no payload), and ``success`` to mark the stream failed.
    """

    def __init__(self) -> None:
        self.abort_with: tuple[grpc.StatusCode, str] | None = None
        self.logs: list[str] = []
        self.swallow_result = False
        self.success = True
        self.chunk_size = 1024 * 1024
        self.delay = 0.0
        self.worker_id = "w-fake"
        self.container_id = "ctr-fake"
        self.envelope: dict[str, object] | None = None
        """Override the response envelope, e.g. to return a remote failure."""
        self.raw_payload: bytes | None = None
        """Send these bytes verbatim, bypassing the envelope entirely."""

        # Everything the client sent, for assertions.
        self.headers: list[proxy_pb2.ClientExecutionRequest] = []
        self.payload_messages: list[proxy_pb2.ClientExecutionRequest] = []
        self.session_ids: list[str] = []
        self.request_ids: list[str] = []
        self.metadata: list[tuple[str, str]] = []
        self.concurrent = 0
        self.max_concurrent = 0

    async def RequestExecution(  # noqa: N802 - the generated interface names it
        self,
        request_iterator: object,
        context: grpc.aio.ServicerContext,
    ) -> object:
        """Collect the request stream, run it, and stream the result back."""
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.metadata = [(k, v) for k, v in context.invocation_metadata() or ()]
        try:
            if self.abort_with is not None:
                code, details = self.abort_with
                await context.abort(code, details)

            payload = bytearray()
            ref = ("", "")
            async for message in request_iterator:  # type: ignore[attr-defined]
                ref = (message.request_id, message.session_id)
                self.request_ids.append(message.request_id)
                self.session_ids.append(message.session_id)
                arm = message.WhichOneof("data")
                if arm == "config":
                    self.headers.append(message)
                elif arm == "payload":
                    self.payload_messages.append(message)
                    payload.extend(message.payload)

            if self.delay:
                await asyncio.sleep(self.delay)

            for line in self.logs:
                await context.write(self._message(ref, logs=line))

            if self.swallow_result:
                return None

            # Mirrors the executor: run the call and wrap the outcome, so a
            # raising function comes back as data rather than as silence.
            if self.envelope is not None:
                envelope = self.envelope
            else:
                call = cloudpickle.loads(bytes(payload))
                try:
                    envelope = {"ok": True, "value": call["func"](*call["args"], **call["kwargs"])}
                except Exception as exc:
                    envelope = {
                        "ok": False,
                        "exc_type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    }
            result = (
                self.raw_payload if self.raw_payload is not None else cloudpickle.dumps(envelope)
            )
            for start in range(0, len(result), self.chunk_size):
                await context.write(
                    self._message(ref, payload=result[start : start + self.chunk_size])
                )
            return None
        finally:
            self.concurrent -= 1

    def _message(
        self,
        ref: tuple[str, str],
        *,
        payload: bytes | None = None,
        logs: str | None = None,
    ) -> proxy_pb2.ClientExecutionResponse:
        message = proxy_pb2.ClientExecutionResponse(
            request_id=ref[0],
            session_id=ref[1],
            success=self.success,
            worker_id=self.worker_id,
            container_id=self.container_id,
        )
        if payload is not None:
            message.payload = payload
        elif logs is not None:
            message.logs = logs
        return message


class RouterHarness:
    """Runs a FakeRouter on an ephemeral port."""

    def __init__(self) -> None:
        self.router = FakeRouter()
        self._server = grpc.aio.server()
        proxy_pb2_grpc.add_ProxyServiceServicer_to_server(self.router, self._server)
        self.port = 0

    async def start(self) -> None:
        """Bind and serve."""
        self.port = self._server.add_insecure_port("127.0.0.1:0")
        await self._server.start()

    async def stop(self) -> None:
        """Stop immediately; tests do not need a drain."""
        await self._server.stop(0)

    @property
    def uri(self) -> str:
        """Where the client should point."""
        return f"127.0.0.1:{self.port}"
