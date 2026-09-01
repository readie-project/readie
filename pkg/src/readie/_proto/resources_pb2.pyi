from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ResourceKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RESOURCE_KIND_UNKNOWN: _ClassVar[ResourceKind]
    RESOURCE_KIND_MEMORY: _ClassVar[ResourceKind]
    RESOURCE_KIND_GPU_MEMORY: _ClassVar[ResourceKind]
RESOURCE_KIND_UNKNOWN: ResourceKind
RESOURCE_KIND_MEMORY: ResourceKind
RESOURCE_KIND_GPU_MEMORY: ResourceKind

class ResourceBudget(_message.Message):
    __slots__ = ("kind", "alloc", "max")
    KIND_FIELD_NUMBER: _ClassVar[int]
    ALLOC_FIELD_NUMBER: _ClassVar[int]
    MAX_FIELD_NUMBER: _ClassVar[int]
    kind: ResourceKind
    alloc: int
    max: int
    def __init__(self, kind: _Optional[_Union[ResourceKind, str]] = ..., alloc: _Optional[int] = ..., max: _Optional[int] = ...) -> None: ...
