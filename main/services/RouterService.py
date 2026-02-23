from generated import router_pb2, router_pb2_grpc


class RouterService(router_pb2_grpc.RouterService):
    async def RequestExecution(self, request, context) -> router_pb2.ClientExecutionResponse:
        print(
            f"Received execution request: {request.function_name} with args {request.args}")
        # For demonstration, we just return a dummy container ID.
        return router_pb2.ClientExecutionResponse(container_id="dummy_container_id")

    async def AssignExecution(self, request, context) -> router_pb2.WorkerExecutionResponse:
        print(
            f"Received execution request: {request.function_name} with args {request.args}")
        # For demonstration, we just return a dummy container ID.
        return router_pb2.WorkerExecutionResponse(container_id="dummy_container_id")
