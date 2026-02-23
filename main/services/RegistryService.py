from generated import registry_pb2, registry_pb2_grpc

class RegistryService(registry_pb2_grpc.RegistryService):
    async def RequestExecution(self, request, context) -> registry_pb2.ClientExecutionResponse:
        print(
            f"Received execution request: {request.function_name} with args {request.args}")
        # For demonstration, we just return a dummy container ID.
        return registry_pb2.ClientExecutionResponse(container_id="dummy_container_id")

    async def AssignExecution(self, request, context) -> registry_pb2.WorkerExecutionResponse:
        print(
            f"Received execution request: {request.function_name} with args {request.args}")
        # For demonstration, we just return a dummy container ID.
        return registry_pb2.WorkerExecutionResponse(container_id="dummy_container_id")