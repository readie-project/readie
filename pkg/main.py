import grpc
import os
import io
import functools
import cloudpickle
from generated import proxy_pb2, proxy_pb2_grpc
from typing import AsyncGenerator
import asyncio

from dotenv import load_dotenv

load_dotenv()

CHUNK_SIZE = 1024 * 1024


class Executor:
    session_id: str

    def __init__(self):
        self.session_id = "session-id"

    def _get_request_iterator(self, request_id: str, pickled_bytes: bytes, resources: proxy_pb2.ResourceEstimation):
        sent_resources = False

        if sent_resources == False:
            sent_resources = True
            yield proxy_pb2.ClientExecutionRequest(resources=resources, request_id=request_id, session_id=self.session_id)

        buffer = io.BytesIO(pickled_bytes)
        while True:
            piece = buffer.read(CHUNK_SIZE)
            if not piece:
                buffer.close()
                break
            yield proxy_pb2.ClientExecutionRequest(payload=piece, request_id=request_id, session_id=self.session_id)

    def parse_tree(self, func, args, kwargs) -> proxy_pb2.ResourceEstimation:
        # TODO: Call tree parser here
        return proxy_pb2.ResourceEstimation(code="", variables=[proxy_pb2.Variables(id="", value="", type="", shape="")], imports=[proxy_pb2.Imports(id="", name="")])

    async def execute(self, pickled_bytes: bytes, resources: proxy_pb2.ResourceEstimation):
        request_id = "request_id"

        # TODO: Single gRPC connection instance
        async with grpc.aio.insecure_channel(os.environ.get("MAIN_NODE_URI")) as channel:
            proxyStub = proxy_pb2_grpc.ProxyServiceStub(channel)
            stream: AsyncGenerator[proxy_pb2.ClientExecutionResponse] = proxyStub.RequestExecution(
                self._get_request_iterator(request_id, pickled_bytes, resources))

            async for response in stream:
                if response.WhichOneof("data") == "payload":
                    yield {"logs": False, "data": response.payload}
                elif response.WhichOneof("data") == "logs":
                    yield {"logs": True, "data": response.logs}


executor = Executor()

# Function decorator for remote execution


def remote(func):
    @functools.wraps(func)
    async def remote_execution(*args, **kwargs):
        pickled_bytes = cloudpickle.dumps({
            'func': func,
            'args': args,
            'kwargs': kwargs
        })

        resources = executor.parse_tree(func, args, kwargs)

        response_pickle_bytes = bytearray()
        async for response in executor.execute(pickled_bytes, resources):
            if response['logs'] == False:
                response_pickle_bytes.extend(response['data'])
            else:
                print(response['data'])

        return cloudpickle.loads(response_pickle_bytes)

    return remote_execution


async def main():
    @remote
    def func(a, b):
        print("Adding A and B")
        return a + b

    result = await func(1, 2)
    print(f"Result: {result}")

if __name__ == "__main__":
    asyncio.run(main())
