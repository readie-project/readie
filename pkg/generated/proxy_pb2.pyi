from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class ClientExecutionRequest(_message.Message):
    __slots__ = ("request_id", "session_id", "payload", "required_resources")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_RESOURCES_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    session_id: str
    payload: bytes
    required_resources: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., payload: _Optional[bytes] = ..., required_resources: _Optional[_Iterable[str]] = ...) -> None: ...

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
