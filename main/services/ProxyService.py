from generated import proxy_pb2, proxy_pb2_grpc


class ProxyService(proxy_pb2_grpc.ProxyService):
    async def RequestExecution(self, request, context) -> proxy_pb2.ClientExecutionResponse:
        print(
            f"Received execution request: {request.function_name} with args {request.args}")
        # For demonstration, we just return a dummy container ID.
        return proxy_pb2.ClientExecutionResponse(container_id="dummy_container_id")
