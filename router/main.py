import asyncio
import logging
import os
import grpc
from grpc_reflection.v1alpha import reflection
from services.ProxyService import ProxyService
from services.RegistryService import RegistryService
from generated import proxy_pb2, proxy_pb2_grpc, registry_pb2, registry_pb2_grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

if os.environ.get("APP_ENV", "") != "production":
    from dotenv import load_dotenv
    load_dotenv()

# Coroutines to be invoked when the event loop is shutting down.
_cleanup_coroutines = []


async def serve() -> None:
    server = grpc.aio.server()

    proxy_pb2_grpc.add_ProxyServiceServicer_to_server(ProxyService(), server)
    registry_pb2_grpc.add_RegistryServiceServicer_to_server(
        RegistryService(), server)
    health_servicer = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

    SERVICE_NAMES = (
        proxy_pb2.DESCRIPTOR.services_by_name['ProxyService'].full_name,
        registry_pb2.DESCRIPTOR.services_by_name['RegistryService'].full_name,
        health.SERVICE_NAME,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(SERVICE_NAMES, server)

    listen_addr = f"{os.environ.get("SERVICE_NAME")}:{os.environ.get("PORT")}"
    server.add_insecure_port(listen_addr)
    logging.info("Starting server on %s", listen_addr)

    await server.start()

    health_servicer.set(
        "", health_pb2.HealthCheckResponse.SERVING)
    logging.info("Health check set to serving")

    async def server_graceful_shutdown():
        logging.info("Starting graceful shutdown...")
        # Shuts down the server with 5 seconds of grace period. During the
        # grace period, the server won't accept new connections and allow
        # existing RPCs to continue within the grace period.
        await server.stop(5)

    _cleanup_coroutines.append(server_graceful_shutdown())
    await server.wait_for_termination()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(serve())
    finally:
        loop.run_until_complete(*_cleanup_coroutines)
        loop.close()
