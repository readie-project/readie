from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class WorkerStatus(_message.Message):
    __slots__ = ("worker_id", "status")
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    worker_id: str
    status: str
    def __init__(self, worker_id: _Optional[str] = ..., status: _Optional[str] = ...) -> None: ...

class ExecutorStatus(_message.Message):
    __slots__ = ("container_id", "worker_id", "status")
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    container_id: str
    worker_id: str
    status: str
    def __init__(self, container_id: _Optional[str] = ..., worker_id: _Optional[str] = ..., status: _Optional[str] = ...) -> None: ...
