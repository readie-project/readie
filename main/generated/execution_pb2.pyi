from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class WorkerExecutionRequest(_message.Message):
    __slots__ = ("request_id", "session_id", "worker_id", "payload", "container_id", "cpu_alloc", "gpu_alloc", "checkpoint_id", "additional_resources")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    CPU_ALLOC_FIELD_NUMBER: _ClassVar[int]
    GPU_ALLOC_FIELD_NUMBER: _ClassVar[int]
    CHECKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    ADDITIONAL_RESOURCES_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    session_id: str
    worker_id: str
    payload: bytes
    container_id: str
    cpu_alloc: float
    gpu_alloc: float
    checkpoint_id: str
    additional_resources: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., worker_id: _Optional[str] = ..., payload: _Optional[bytes] = ..., container_id: _Optional[str] = ..., cpu_alloc: _Optional[float] = ..., gpu_alloc: _Optional[float] = ..., checkpoint_id: _Optional[str] = ..., additional_resources: _Optional[_Iterable[str]] = ...) -> None: ...

class WorkerExecutionResponse(_message.Message):
    __slots__ = ("request_id", "session_id", "worker_id", "container_id", "success", "stdout", "stderr")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    STDOUT_FIELD_NUMBER: _ClassVar[int]
    STDERR_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    session_id: str
    worker_id: str
    container_id: str
    success: bool
    stdout: str
    stderr: str
    def __init__(self, request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., worker_id: _Optional[str] = ..., container_id: _Optional[str] = ..., success: bool = ..., stdout: _Optional[str] = ..., stderr: _Optional[str] = ...) -> None: ...
