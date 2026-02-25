from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Status(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    UNKNOWN: _ClassVar[Status]
    IDLE: _ClassVar[Status]
    BUSY: _ClassVar[Status]
    ERROR: _ClassVar[Status]
UNKNOWN: Status
IDLE: Status
BUSY: Status
ERROR: Status

class WorkerStatus(_message.Message):
    __slots__ = ("worker_id", "status")
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    worker_id: str
    status: Status
    def __init__(self, worker_id: _Optional[str] = ..., status: _Optional[_Union[Status, str]] = ...) -> None: ...

class ExecutorStatus(_message.Message):
    __slots__ = ("container_id", "worker_id", "status")
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    container_id: str
    worker_id: str
    status: Status
    def __init__(self, container_id: _Optional[str] = ..., worker_id: _Optional[str] = ..., status: _Optional[_Union[Status, str]] = ...) -> None: ...

class WorkerUtilization(_message.Message):
    __slots__ = ("worker_id", "cpu_util", "cpu_total", "gpu_util", "gpu_total")
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    CPU_UTIL_FIELD_NUMBER: _ClassVar[int]
    CPU_TOTAL_FIELD_NUMBER: _ClassVar[int]
    GPU_UTIL_FIELD_NUMBER: _ClassVar[int]
    GPU_TOTAL_FIELD_NUMBER: _ClassVar[int]
    worker_id: str
    cpu_util: float
    cpu_total: float
    gpu_util: float
    gpu_total: float
    def __init__(self, worker_id: _Optional[str] = ..., cpu_util: _Optional[float] = ..., cpu_total: _Optional[float] = ..., gpu_util: _Optional[float] = ..., gpu_total: _Optional[float] = ...) -> None: ...

class ExecutorUtilization(_message.Message):
    __slots__ = ("container_id", "worker_id", "cpu_util", "cpu_total", "gpu_util", "gpu_total")
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    CPU_UTIL_FIELD_NUMBER: _ClassVar[int]
    CPU_TOTAL_FIELD_NUMBER: _ClassVar[int]
    GPU_UTIL_FIELD_NUMBER: _ClassVar[int]
    GPU_TOTAL_FIELD_NUMBER: _ClassVar[int]
    container_id: str
    worker_id: str
    cpu_util: float
    cpu_total: float
    gpu_util: float
    gpu_total: float
    def __init__(self, container_id: _Optional[str] = ..., worker_id: _Optional[str] = ..., cpu_util: _Optional[float] = ..., cpu_total: _Optional[float] = ..., gpu_util: _Optional[float] = ..., gpu_total: _Optional[float] = ...) -> None: ...

class RegistryUpdateResponse(_message.Message):
    __slots__ = ("updated",)
    UPDATED_FIELD_NUMBER: _ClassVar[int]
    updated: bool
    def __init__(self, updated: bool = ...) -> None: ...
