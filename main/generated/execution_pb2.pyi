from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class WorkerExecutionRequest(_message.Message):
    __slots__ = ("request_id", "payload", "worker_id", "container_id")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    payload: bytes
    worker_id: str
    container_id: str
    def __init__(self, request_id: _Optional[str] = ..., payload: _Optional[bytes] = ..., worker_id: _Optional[str] = ..., container_id: _Optional[str] = ...) -> None: ...

class WorkerExecutionResponse(_message.Message):
    __slots__ = ("request_id", "stdout", "stderr")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    STDOUT_FIELD_NUMBER: _ClassVar[int]
    STDERR_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    stdout: str
    stderr: str
    def __init__(self, request_id: _Optional[str] = ..., stdout: _Optional[str] = ..., stderr: _Optional[str] = ...) -> None: ...
