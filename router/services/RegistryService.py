from generated import registry_pb2, registry_pb2_grpc
from scheduler import get_scheduler


class RegistryService(registry_pb2_grpc.RegistryService):
    def PostWorkerStatus(self, request: registry_pb2.WorkerStatus, context) -> registry_pb2.RegistryUpdateResponse:
        print(
            f"Received worker status: {request.worker_id} - {request.status}")
        get_scheduler().update_worker_status(request)
        return registry_pb2.RegistryUpdateResponse(updated=True)

    def PostExecutorStatus(self, request: registry_pb2.ExecutorStatus, context) -> registry_pb2.RegistryUpdateResponse:
        print(
            f"Received executor status: {request.worker_id}~{request.container_id} - {request.status}")
        get_scheduler().update_executor_status(request)
        return registry_pb2.RegistryUpdateResponse(updated=True)
    
    def PostWorkerUtilization(self, request: registry_pb2.WorkerUtilization, context) -> registry_pb2.RegistryUpdateResponse:
        print(
            f"Received worker utilization update: {request.worker_id}")
        get_scheduler().update_worker_utilization(request)
        return registry_pb2.RegistryUpdateResponse(updated=True)
    
    def PostExecutorUtilization(self, request: registry_pb2.WorkerUtilization, context) -> registry_pb2.RegistryUpdateResponse:
        print(
            f"Received executor utilization update: {request.worker_id}")
        get_scheduler().update_executor_utilization(request)
        return registry_pb2.RegistryUpdateResponse(updated=True)
