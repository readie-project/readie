from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class WorkerExecutionRequest(_message.Message):
    __slots__ = ("request_id", "session_id", "worker_id", "container_id", "checkpoint_id", "payload", "cpu_alloc", "gpu_alloc", "resources")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    CHECKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    CPU_ALLOC_FIELD_NUMBER: _ClassVar[int]
    GPU_ALLOC_FIELD_NUMBER: _ClassVar[int]
    RESOURCES_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    session_id: str
    worker_id: str
    container_id: str
    checkpoint_id: str
    payload: bytes
    cpu_alloc: int
    gpu_alloc: int
    resources: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., worker_id: _Optional[str] = ..., container_id: _Optional[str] = ..., checkpoint_id: _Optional[str] = ..., payload: _Optional[bytes] = ..., cpu_alloc: _Optional[int] = ..., gpu_alloc: _Optional[int] = ..., resources: _Optional[_Iterable[str]] = ...) -> None: ...

class WorkerExecutionResponse(_message.Message):
    __slots__ = ("request_id", "session_id", "worker_id", "container_id", "checkpoint_id", "cpu_alloc", "gpu_alloc", "success", "logs", "payload")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    CHECKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    CPU_ALLOC_FIELD_NUMBER: _ClassVar[int]
    GPU_ALLOC_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    LOGS_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    session_id: str
    worker_id: str
    container_id: str
    checkpoint_id: str
    cpu_alloc: int
    gpu_alloc: int
    success: bool
    logs: str
    payload: bytes
    def __init__(self, request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., worker_id: _Optional[str] = ..., container_id: _Optional[str] = ..., checkpoint_id: _Optional[str] = ..., cpu_alloc: _Optional[int] = ..., gpu_alloc: _Optional[int] = ..., success: _Optional[bool] = ..., logs: _Optional[str] = ..., payload: _Optional[bytes] = ...) -> None: ...
