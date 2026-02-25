import grpc
from generated import execution_pb2, execution_pb2_grpc


class Scheduler:
    state_table = None

    def __init__(self):
        self.state_table = {}

    def update_state(self, function_name, state):
        self.state_table[function_name] = state

    async def execute(self):
        print("Scheduler executing...")
        async with grpc.aio.insecure_channel("localhost:50052") as channel:
            stub = execution_pb2_grpc.ExecutionServiceStub(channel)

            async def request_generator():
                yield execution_pb2.WorkerExecutionRequest(request_id="req1", worker_id="worker1")
                print("Sent yield request to Go server.")

            stream = stub.RequestExecution(request_generator())
            print("Sent execution request to Go server.")

            async for response in stream:
                print(f"Received from Go: {response.stdout}")
                yield response
