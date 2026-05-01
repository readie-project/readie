from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Variables(_message.Message):
    __slots__ = ("id", "value", "ctx", "type", "shape")
    ID_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    CTX_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    SHAPE_FIELD_NUMBER: _ClassVar[int]
    id: str
    value: str
    ctx: str
    type: str
    shape: str
    def __init__(self, id: _Optional[str] = ..., value: _Optional[str] = ..., ctx: _Optional[str] = ..., type: _Optional[str] = ..., shape: _Optional[str] = ...) -> None: ...

class Imports(_message.Message):
    __slots__ = ("id", "name")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class ResourceEstimation(_message.Message):
    __slots__ = ("code", "variables", "imports")
    CODE_FIELD_NUMBER: _ClassVar[int]
    VARIABLES_FIELD_NUMBER: _ClassVar[int]
    IMPORTS_FIELD_NUMBER: _ClassVar[int]
    code: str
    variables: _containers.RepeatedCompositeFieldContainer[Variables]
    imports: _containers.RepeatedCompositeFieldContainer[Imports]
    def __init__(self, code: _Optional[str] = ..., variables: _Optional[_Iterable[_Union[Variables, _Mapping]]] = ..., imports: _Optional[_Iterable[_Union[Imports, _Mapping]]] = ...) -> None: ...

class ClientExecutionRequest(_message.Message):
    __slots__ = ("request_id", "session_id", "resources", "payload")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    RESOURCES_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    session_id: str
    resources: ResourceEstimation
    payload: bytes
    def __init__(self, request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., resources: _Optional[_Union[ResourceEstimation, _Mapping]] = ..., payload: _Optional[bytes] = ...) -> None: ...

class ClientExecutionResponse(_message.Message):
    __slots__ = ("request_id", "session_id", "success", "logs", "payload")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    LOGS_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    session_id: str
    success: bool
    logs: str
    payload: bytes
    def __init__(self, request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., success: bool = ..., logs: _Optional[str] = ..., payload: _Optional[bytes] = ...) -> None: ...
