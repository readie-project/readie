from readie._proto import resources_pb2 as _resources_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ExecutionConfig(_message.Message):
    __slots__ = ("imports", "budgets", "gpu", "disable_optimized_execution")
    IMPORTS_FIELD_NUMBER: _ClassVar[int]
    BUDGETS_FIELD_NUMBER: _ClassVar[int]
    GPU_FIELD_NUMBER: _ClassVar[int]
    DISABLE_OPTIMIZED_EXECUTION_FIELD_NUMBER: _ClassVar[int]
    imports: _containers.RepeatedScalarFieldContainer[str]
    budgets: _containers.RepeatedCompositeFieldContainer[_resources_pb2.ResourceBudget]
    gpu: bool
    disable_optimized_execution: bool
    def __init__(self, imports: _Optional[_Iterable[str]] = ..., budgets: _Optional[_Iterable[_Union[_resources_pb2.ResourceBudget, _Mapping]]] = ..., gpu: _Optional[bool] = ..., disable_optimized_execution: _Optional[bool] = ...) -> None: ...

class ClientExecutionRequest(_message.Message):
    __slots__ = ("request_id", "session_id", "payload", "config")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    session_id: str
    payload: bytes
    config: ExecutionConfig
    def __init__(self, request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., payload: _Optional[bytes] = ..., config: _Optional[_Union[ExecutionConfig, _Mapping]] = ...) -> None: ...

class ClientExecutionResponse(_message.Message):
    __slots__ = ("request_id", "session_id", "success", "logs", "payload", "worker_id", "container_id")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    LOGS_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    session_id: str
    success: bool
    logs: str
    payload: bytes
    worker_id: str
    container_id: str
    def __init__(self, request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., success: _Optional[bool] = ..., logs: _Optional[str] = ..., payload: _Optional[bytes] = ..., worker_id: _Optional[str] = ..., container_id: _Optional[str] = ...) -> None: ...
