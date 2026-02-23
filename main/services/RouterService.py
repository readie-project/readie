from generated import router_pb2, router_pb2_grpc


class RouterService(router_pb2_grpc.RouterService):
    async def PostWorkerStatus(self, request, context) -> router_pb2.Empty:
        print(
            f"Received worker status: {request.worker_id} - {request.status}")
        return router_pb2.Empty()

    async def PostExecutorStatus(self, request, context) -> router_pb2.Empty:
        print(
            f"Received executor status: {request.container_id} - {request.worker_id} - {request.status}")
        return router_pb2.Empty()
