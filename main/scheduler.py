from generated import proxy_pb2, registry_pb2, execution_pb2, execution_pb2_grpc
from typing import TypedDict


class Executor(TypedDict):
    checkpoint_id: str
    status: registry_pb2.Status
    cpu_util: int
    cpu_total: int
    gpu_util: int
    gpu_total: int


class Worker(TypedDict):
    worker_uri: str
    status: registry_pb2.Status
    executors: dict[str, Executor]
    cpu_util: int
    cpu_total: int
    gpu_util: int
    gpu_total: int


class Session(TypedDict):
    container_id: str
    requests: set[str]


class Scheduler:
    sessions: dict[str, Session]
    workers: dict[str, Worker]

    def __init__(self):
        self.sessions = {}
        self.workers = {}

    def _create_worker_if_not_exists(self, worker_id: str):
        if worker_id not in self.workers:
            self.workers[worker_id] = {
                'worker_uri': "",
                'status': registry_pb2.STATUS_UNKNOWN,
                'executors': {},
                'cpu_util': 0,
                'cpu_total': 0,
                'gpu_util': 0,
                'gpu_total': 0,
            }
            return True
        return False

    def _create_executor_if_not_exists(self, worker_id: str, container_id: str):
        self._create_worker_if_not_exists(worker_id)
        if container_id not in self.workers[worker_id]["executors"]:
            self.workers[worker_id]["executors"][container_id] = {
                'status': registry_pb2.STATUS_UNKNOWN,
                'checkpoint_id'
                'cpu_util': 0,
                'cpu_total': 0,
                'gpu_util': 0,
                'gpu_total': 0,
            }
            return True
        return False

    def _create_session_if_not_exists(self, session_id: str):
        if session_id not in self.sessions:
            self.sessions[session_id] = {
                'container_id': "",
                'requests': []
            }
            return True
        return False

    def add_request_to_session(self, session_id: str, request_id: str):
        self._create_session_if_not_exists(session_id)
        self.sessions[session_id]['requests'].add(request_id)

    def update_worker_status(self, request: registry_pb2.WorkerStatus):
        self._create_worker_if_not_exists(request.worker_id)
        self.workers[request.worker_id].update(
            worker_uri=request.worker_uri, status=request.status)

    def update_executor_status(self, request: registry_pb2.ExecutorStatus):
        self._create_executor_if_not_exists(
            request.worker_id, request.container_id)
        self.workers[request.worker_id]['executors'][request.container_id].update(
            status=request.status)
        self._create_session_if_not_exists(request.session_id)
        self.sessions[request.session_id].update(
            container_id=request.container_id)

    def update_worker_utilization(self, request: registry_pb2.WorkerUtilization):
        self._create_worker_if_not_exists(request.worker_id)
        self.workers[request.worker_id]['executors'][request.container_id].update(
            cpu_util=request.cpu_util, cpu_total=request.cpu_total, gpu_util=request.gpu_util, gpu_total=request.gpu_total)

    def update_executor_utilization(self, request: registry_pb2.ExecutorUtilization):
        self._create_executor_if_not_exists(
            request.worker_id, request.container_id)
        self.workers[request.worker_id]['executors'][request.container_id].update(
            cpu_util=request.cpu_util, cpu_total=request.cpu_total, gpu_util=request.gpu_util, gpu_total=request.gpu_total)

    def update_executor_provision(self, response: execution_pb2.WorkerExecutionResponse):
        self._create_executor_if_not_exists(
            response.worker_id, response.container_id)
        self.workers[response.worker_id]['executors'][response.container_id].update(
            checkpoint_id=response.checkpoint_id, cpu_total=response.cpu_alloc, gpu_total=response.gpu_alloc)
        self._create_session_if_not_exists(response.session_id)
        self.sessions[response.session_id].update(
            container_id=response.container_id)

    def provision(self, request: proxy_pb2.ClientExecutionRequest):
        worker_id = "worker-1"
        worker_uri = self.workers[worker_id]['worker_uri']
        container_id = ""
        checkpoint_id = ""
        cpu_alloc = 512 * 1024 * 1024
        gpu_alloc = 0

        return {
            "cpu_alloc": cpu_alloc,
            "gpu_alloc": gpu_alloc,
            "worker_id": worker_id,
            "worker_uri": worker_uri,
            "container_id": container_id,
            "checkpoint_id": checkpoint_id,
        }


scheduler = Scheduler()


def get_scheduler():
    return scheduler
