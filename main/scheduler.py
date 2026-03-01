import grpc
from generated import proxy_pb2, execution_pb2, execution_pb2_grpc
import os


class Scheduler:
    state_table = None

    def __init__(self):
        self.state_table = {}
        self.workers = {}

    def update_state(self, function_name, state):
        self.state_table[function_name] = state

    async def provision(self, request: proxy_pb2.ClientExecutionRequest):
        worker_id = ""
        container_id = ""
        checkpoint_id = ""
        cpu_alloc = 0
        gpu_alloc = 0

        if container_id == "":
            print("Requesting provision of new executor")
            async with grpc.aio.insecure_channel(os.environ.get("WORKER_NODE_URI")) as channel:
                stub = execution_pb2_grpc.ExecutionServiceStub(channel)
                response: execution_pb2.ExecutorProvisionResponse = stub.RequestProvision(execution_pb2.ExecutorProvisionRequest(
                    request_id=request.request_id, session_id=request.session_id, checkpoint_id=checkpoint_id, cpu_alloc=cpu_alloc, gpu_alloc=gpu_alloc))

        return {
            "cpu_alloc": response.cpu_alloc,
            "gpu_alloc": response.gpu_alloc,
            "worker_id": worker_id,
            "container_id": response.container_id,
            "checkpoint_id": response.checkpoint_id,
            "additional_resources": []
        }


scheduler = Scheduler()


def get_scheduler():
    return scheduler
