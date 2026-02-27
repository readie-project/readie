import cloudpickle
import asyncio
import logging
import os
import threading
import time
import grpc
from services.ProxyService import ProxyService
from services.RegistryService import RegistryService
from generated import proxy_pb2_grpc, registry_pb2_grpc
from generated import execution_pb2, execution_pb2_grpc

from dotenv import load_dotenv

load_dotenv()

# Coroutines to be invoked when the event loop is shutting down.
_cleanup_coroutines = []


def create_cloudpickle_file():
    def sample_function(x):
        return x * x

    with open("test.pkl", "wb") as f:
        cloudpickle.dump({
            'func': sample_function,
            'args': [12],
            'kwargs': {}
        }, f)


def get_file_chunks(filename):
    request_id = "req1"
    session_id = "sess1"
    worker_id = "worker1"
    cpu_alloc = 1024 * 1024 * 1024  # 512MB

    CHUNK_SIZE = 1024 * 1024  # 1MB chunks
    with open(filename, 'rb') as f:
        while True:
            piece = f.read(CHUNK_SIZE)
            if not piece:
                break
            yield execution_pb2.WorkerExecutionRequest(payload=piece, request_id=request_id, worker_id=worker_id, session_id=session_id, cpu_alloc=cpu_alloc)


async def request_execution():
    create_cloudpickle_file()
    print("Requesting execution from Go server...")
    async with grpc.aio.insecure_channel(os.environ.get("WORKER_NODE_URI")) as channel:
        stub = execution_pb2_grpc.ExecutionServiceStub(channel)

        stream = stub.RequestExecution(get_file_chunks("./test.pkl"))
        print("Sent execution request to Go server.")

        async for response in stream:
            print(f"Received from Go: {response}")
            yield response


async def serve() -> None:
    server = grpc.aio.server()
    proxy_pb2_grpc.add_ProxyServiceServicer_to_server(ProxyService(), server)
    registry_pb2_grpc.add_RegistryServiceServicer_to_server(
        RegistryService(), server)

    listen_addr = os.environ.get("MAIN_NODE_URI", None)
    server.add_insecure_port(listen_addr)
    logging.info("Starting server on %s", listen_addr)
    await server.start()

    async def server_graceful_shutdown():
        logging.info("Starting graceful shutdown...")
        # Shuts down the server with 5 seconds of grace period. During the
        # grace period, the server won't accept new connections and allow
        # existing RPCs to continue within the grace period.
        await server.stop(5)

    _cleanup_coroutines.append(server_graceful_shutdown())
    await server.wait_for_termination()


async def run():
    async for response in request_execution():
        print(f"Got response: {response}")

if __name__ == "__main__":
    # logging.basicConfig(level=logging.INFO)
    # loop = asyncio.get_event_loop()
    # try:
    #     loop.run_until_complete(serve())
    # finally:
    #     loop.run_until_complete(*_cleanup_coroutines)
    #     loop.close()

    # time.sleep(10)  # Wait for the server to start

    asyncio.run(run())
