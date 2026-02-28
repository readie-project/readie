from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class ClientExecutionRequest(_message.Message):
    __slots__ = ("request_id", "session_id", "payload")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    session_id: str
    payload: bytes
    def __init__(self, request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., payload: _Optional[bytes] = ...) -> None: ...

class ClientExecutionResponse(_message.Message):
    __slots__ = ("request_id", "session_id", "success", "stdout", "stderr", "payload")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    STDOUT_FIELD_NUMBER: _ClassVar[int]
    STDERR_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    session_id: str
    success: bool
    stdout: str
    stderr: str
    payload: bytes
    def __init__(self, request_id: _Optional[str] = ..., session_id: _Optional[str] = ..., success: bool = ..., stdout: _Optional[str] = ..., stderr: _Optional[str] = ..., payload: _Optional[bytes] = ...) -> None: ...
