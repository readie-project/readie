from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Action(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ACTION_UNKNOWN: _ClassVar[Action]
    ACTION_START: _ClassVar[Action]
    ACTION_RESUME: _ClassVar[Action]
    ACTION_RESTART: _ClassVar[Action]
ACTION_UNKNOWN: Action
ACTION_START: Action
ACTION_RESUME: Action
ACTION_RESTART: Action

class WorkerExecutionRequest(_message.Message):
    __slots__ = ("container_id", "checkpoint_id", "payload", "cpu_alloc", "gpu_alloc", "required_resources", "additional_resources", "action")
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    CHECKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    CPU_ALLOC_FIELD_NUMBER: _ClassVar[int]
    GPU_ALLOC_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_RESOURCES_FIELD_NUMBER: _ClassVar[int]
    ADDITIONAL_RESOURCES_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    container_id: str
    checkpoint_id: str
    payload: bytes
    cpu_alloc: int
    gpu_alloc: int
    required_resources: _containers.RepeatedScalarFieldContainer[str]
    additional_resources: _containers.RepeatedScalarFieldContainer[str]
    action: Action
    def __init__(self, container_id: _Optional[str] = ..., checkpoint_id: _Optional[str] = ..., payload: _Optional[bytes] = ..., cpu_alloc: _Optional[int] = ..., gpu_alloc: _Optional[int] = ..., required_resources: _Optional[_Iterable[str]] = ..., additional_resources: _Optional[_Iterable[str]] = ..., action: _Optional[_Union[Action, str]] = ...) -> None: ...

class WorkerExecutionResponse(_message.Message):
    __slots__ = ("container_id", "success", "stdout", "stderr")
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    STDOUT_FIELD_NUMBER: _ClassVar[int]
    STDERR_FIELD_NUMBER: _ClassVar[int]
    container_id: str
    success: bool
    stdout: str
    stderr: str
    def __init__(self, container_id: _Optional[str] = ..., success: bool = ..., stdout: _Optional[str] = ..., stderr: _Optional[str] = ...) -> None: ...

class ExecutorProvisionRequest(_message.Message):
    __slots__ = ("checkpoint_id", "cpu_alloc", "gpu_alloc")
    CHECKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    CPU_ALLOC_FIELD_NUMBER: _ClassVar[int]
    GPU_ALLOC_FIELD_NUMBER: _ClassVar[int]
    checkpoint_id: str
    cpu_alloc: int
    gpu_alloc: int
    def __init__(self, checkpoint_id: _Optional[str] = ..., cpu_alloc: _Optional[int] = ..., gpu_alloc: _Optional[int] = ...) -> None: ...

class ExecutorProvisionResponse(_message.Message):
    __slots__ = ("container_id", "cpu_alloc", "gpu_alloc")
    CONTAINER_ID_FIELD_NUMBER: _ClassVar[int]
    CPU_ALLOC_FIELD_NUMBER: _ClassVar[int]
    GPU_ALLOC_FIELD_NUMBER: _ClassVar[int]
    container_id: str
    cpu_alloc: int
    gpu_alloc: int
    def __init__(self, container_id: _Optional[str] = ..., cpu_alloc: _Optional[int] = ..., gpu_alloc: _Optional[int] = ...) -> None: ...
