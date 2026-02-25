from generated import registry_pb2, registry_pb2_grpc
from scheduler import Scheduler


class RegistryService(registry_pb2_grpc.RegistryService):
    async def PostWorkerStatus(self, request, context) -> registry_pb2.WorkerStatus:
        print(
            f"Received worker status: {request.worker_id} - {request.status}")
        scheduler = Scheduler()
        async for response in scheduler.execute():
            print(f"Got response: {response.stdout}")

        return registry_pb2.WorkerStatus(worker_id=request.worker_id, status=request.status)

    async def PostExecutorStatus(self, request, context) -> registry_pb2.ExecutorStatus:
        print(
            f"Received executor status: {request.container_id} - {request.worker_id} - {request.status}")
        return registry_pb2.ExecutorStatus(container_id=request.container_id, worker_id=request.worker_id, status=request.status)
