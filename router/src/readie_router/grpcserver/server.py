"""The gRPC server: listener, health, reflection and a real drain."""

from __future__ import annotations

from collections.abc import Sequence

import grpc
import structlog
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection

from readie_router.proto import proxy_pb2, proxy_pb2_grpc, registry_pb2, registry_pb2_grpc


class RouterServer:
    """Owns the inbound gRPC server and its lifecycle.

    Health starts NOT_SERVING and is flipped only once the router can genuinely
    serve, so an orchestrator never routes to a half-initialised process. The
    previous implementation set SERVING immediately after start and never set
    anything else, including on the way down.
    """

    def __init__(
        self,
        proxy: proxy_pb2_grpc.ProxyServiceServicer,
        registry: registry_pb2_grpc.RegistryServiceServicer,
        *,
        listen_addr: str,
        max_concurrent_rpcs: int | None = None,
        max_message_bytes: int = 16 * 1024 * 1024,
        interceptors: Sequence[grpc.aio.ServerInterceptor] = (),
    ) -> None:
        self._log = structlog.get_logger("grpcserver.server")
        self._listen_addr = listen_addr
        self._server = grpc.aio.server(
            maximum_concurrent_rpcs=max_concurrent_rpcs,
            interceptors=list(interceptors),
            options=[
                ("grpc.max_send_message_length", max_message_bytes),
                ("grpc.max_receive_message_length", max_message_bytes),
            ],
        )

        proxy_pb2_grpc.add_ProxyServiceServicer_to_server(proxy, self._server)
        registry_pb2_grpc.add_RegistryServiceServicer_to_server(registry, self._server)

        self._health = health.HealthServicer()
        health_pb2_grpc.add_HealthServicer_to_server(self._health, self._server)

        # Reflection is what lets the compose healthcheck's grpcurl work without
        # a descriptor. The service names are deliberately bare: the protos
        # declare no package, and the worker and the healthcheck both depend on
        # that.
        reflection.enable_server_reflection(
            (
                proxy_pb2.DESCRIPTOR.services_by_name["ProxyService"].full_name,
                registry_pb2.DESCRIPTOR.services_by_name["RegistryService"].full_name,
                health.SERVICE_NAME,
                reflection.SERVICE_NAME,
            ),
            self._server,
        )

        self._port = 0

    @property
    def port(self) -> int:
        """The bound port.

        Meaningful only after ``start``. This is how a caller that bound port 0
        discovers which ephemeral port it got.
        """
        return self._port

    async def start(self) -> None:
        """Bind and begin serving, still reporting NOT_SERVING."""
        self._port = self._server.add_insecure_port(self._listen_addr)
        if self._port == 0:
            msg = f"could not bind {self._listen_addr}"
            raise RuntimeError(msg)

        await self._server.start()
        self.set_serving(serving=False)
        self._log.info(
            "gRPC server listening",
            addr=self._listen_addr,
            port=self._port,
        )

    def set_serving(self, *, serving: bool) -> None:
        """Mark the router ready, or not, for traffic."""
        self._health.set(
            "",
            health_pb2.HealthCheckResponse.SERVING
            if serving
            else health_pb2.HealthCheckResponse.NOT_SERVING,
        )

    async def wait(self) -> None:
        """Block until the server stops."""
        await self._server.wait_for_termination()

    async def stop(self, grace: float) -> None:
        """Drain in-flight calls, then stop.

        Health goes NOT_SERVING first so a load balancer stops sending new work
        while existing executions finish.
        """
        self.set_serving(serving=False)
        self._log.info("draining", grace=grace)
        await self._server.stop(grace)
        self._log.info("gRPC server stopped")
