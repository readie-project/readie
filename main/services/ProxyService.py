import grpc
import os
from typing import Iterable, AsyncGenerator
from generated import proxy_pb2, proxy_pb2_grpc, execution_pb2, execution_pb2_grpc
from scheduler import get_scheduler


class ProxyService(proxy_pb2_grpc.ProxyService):
    async def RequestExecution(self, request_iterator: Iterable[proxy_pb2.ClientExecutionRequest], context) -> AsyncGenerator[proxy_pb2.ClientExecutionResponse]:
        print("Execution requested")

        if len(request_iterator) == 0:
            return

        config = get_scheduler().provision(request_iterator[0])

        transformed_request_iterator = (
            execution_pb2.WorkerExecutionRequest(
                payload=request.payload, request_id=request.request_id, session_id=request.session_id, worker_id=config["worker_id"], container_id=config["container_id"], checkpoint_id=config["checkpoint_id"], cpu_alloc=config["cpu_alloc"], gpu_alloc=config["gpu_alloc"], required_resources=request.required_resources, additional_resources=config["additional_resources"])
            for request in request_iterator
        )

        async with grpc.aio.insecure_channel(os.environ.get("WORKER_NODE_URI")) as channel:
            stub = execution_pb2_grpc.ExecutionServiceStub(channel)

            stream: AsyncGenerator[execution_pb2.WorkerExecutionResponse] = stub.RequestExecution(
                transformed_request_iterator)
            print("Sent execution request to worker")

            updated_registry = False
            async for response in stream:
                print(f"Received response from worker")
                if updated_registry == False:
                    get_scheduler().update_executor_provision(response)
                    updated_registry = True

                if response.WhichOneof("data") == "logs":
                    yield proxy_pb2.ClientExecutionResponse(logs=response.logs, request_id=response.request_id, session_id=response.session_id)
                elif response.WhichOneof("data") == "payload":
                    yield proxy_pb2.ClientExecutionResponse(payload=response.payload, request_id=response.request_id, session_id=response.session_id)
