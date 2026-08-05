from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Status(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STATUS_UNKNOWN: _ClassVar[Status]
    STATUS_READY: _ClassVar[Status]
    STATUS_BUSY: _ClassVar[Status]
    STATUS_ERROR: _ClassVar[Status]
    STATUS_REMOVED: _ClassVar[Status]
STATUS_UNKNOWN: Status
STATUS_READY: Status
STATUS_BUSY: Status
STATUS_ERROR: Status
STATUS_REMOVED: Status

class WorkerStatus(_message.Message):
    __slots__ = ("worker_id", "worker_uri", "status", "mem_total", "max_executors", "flavor", "gpu_mem_total")
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    WORKER_URI_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MEM_TOTAL_FIELD_NUMBER: _ClassVar[int]
    MAX_EXECUTORS_FIELD_NUMBER: _ClassVar[int]
    FLAVOR_FIELD_NUMBER: _ClassVar[int]
    GPU_MEM_TOTAL_FIELD_NUMBER: _ClassVar[int]
    worker_id: str
    worker_uri: str
    status: Status
    mem_total: int
    max_executors: int
    flavor: str
    gpu_mem_total: int
    def __init__(self, worker_id: _Optional[str] = ..., worker_uri: _Optional[str] = ..., status: _Optional[_Union[Status, str]] = ..., mem_total: _Optional[int] = ..., max_executors: _Optional[int] = ..., flavor: _Optional[str] = ..., gpu_mem_total: _Optional[int] = ...) -> None: ...

class ExecutorStatus(_message.Message):
    __slots__ = ("container_id", "worker_id", "request_id", "session_id", "status")
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    container_id: str
    worker_id: str
    request_id: str
    session_id: str
    status: Status
    def __init__(self, container_id: _Optional[str] = ..., worker_id: _Optional[str] = ..., request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., status: _Optional[_Union[Status, str]] = ...) -> None: ...

class WorkerUtilization(_message.Message):
    __slots__ = ("worker_id", "cpu_util", "cpu_total", "gpu_util", "gpu_total", "mem_used", "mem_total", "executor_count", "gpu_mem_used", "gpu_mem_total")
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    CPU_UTIL_FIELD_NUMBER: _ClassVar[int]
    CPU_TOTAL_FIELD_NUMBER: _ClassVar[int]
    GPU_UTIL_FIELD_NUMBER: _ClassVar[int]
    GPU_TOTAL_FIELD_NUMBER: _ClassVar[int]
    MEM_USED_FIELD_NUMBER: _ClassVar[int]
    MEM_TOTAL_FIELD_NUMBER: _ClassVar[int]
    EXECUTOR_COUNT_FIELD_NUMBER: _ClassVar[int]
    GPU_MEM_USED_FIELD_NUMBER: _ClassVar[int]
    GPU_MEM_TOTAL_FIELD_NUMBER: _ClassVar[int]
    worker_id: str
    cpu_util: int
    cpu_total: int
    gpu_util: int
    gpu_total: int
    mem_used: int
    mem_total: int
    executor_count: int
    gpu_mem_used: int
    gpu_mem_total: int
    def __init__(self, worker_id: _Optional[str] = ..., cpu_util: _Optional[int] = ..., cpu_total: _Optional[int] = ..., gpu_util: _Optional[int] = ..., gpu_total: _Optional[int] = ..., mem_used: _Optional[int] = ..., mem_total: _Optional[int] = ..., executor_count: _Optional[int] = ..., gpu_mem_used: _Optional[int] = ..., gpu_mem_total: _Optional[int] = ...) -> None: ...

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
    cpu_util: int
    cpu_total: int
    gpu_util: int
    gpu_total: int
    def __init__(self, container_id: _Optional[str] = ..., worker_id: _Optional[str] = ..., cpu_util: _Optional[int] = ..., cpu_total: _Optional[int] = ..., gpu_util: _Optional[int] = ..., gpu_total: _Optional[int] = ...) -> None: ...

class RegistryUpdateResponse(_message.Message):
    __slots__ = ("updated",)
    UPDATED_FIELD_NUMBER: _ClassVar[int]
    updated: bool
    def __init__(self, updated: _Optional[bool] = ...) -> None: ...
