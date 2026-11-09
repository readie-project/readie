from readie._proto import resources_pb2 as _resources_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ClientReportOutcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CLIENT_REPORT_OUTCOME_UNKNOWN: _ClassVar[ClientReportOutcome]
    CLIENT_REPORT_OUTCOME_SUCCESS: _ClassVar[ClientReportOutcome]
    CLIENT_REPORT_OUTCOME_ERROR: _ClassVar[ClientReportOutcome]
    CLIENT_REPORT_OUTCOME_CANCELLED: _ClassVar[ClientReportOutcome]

class ClientReportError(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CLIENT_REPORT_ERROR_UNKNOWN: _ClassVar[ClientReportError]
    CLIENT_REPORT_ERROR_REMOTE_EXECUTION: _ClassVar[ClientReportError]
    CLIENT_REPORT_ERROR_EMPTY_RESULT: _ClassVar[ClientReportError]
    CLIENT_REPORT_ERROR_SERIALIZATION: _ClassVar[ClientReportError]
    CLIENT_REPORT_ERROR_TIMEOUT: _ClassVar[ClientReportError]
    CLIENT_REPORT_ERROR_AUTHORIZATION: _ClassVar[ClientReportError]
    CLIENT_REPORT_ERROR_INVALID_REQUEST: _ClassVar[ClientReportError]
    CLIENT_REPORT_ERROR_RESOURCE_EXHAUSTED: _ClassVar[ClientReportError]
    CLIENT_REPORT_ERROR_SESSION_EXPIRED: _ClassVar[ClientReportError]
    CLIENT_REPORT_ERROR_UNAVAILABLE: _ClassVar[ClientReportError]
    CLIENT_REPORT_ERROR_TRANSPORT: _ClassVar[ClientReportError]
    CLIENT_REPORT_ERROR_CLIENT: _ClassVar[ClientReportError]
CLIENT_REPORT_OUTCOME_UNKNOWN: ClientReportOutcome
CLIENT_REPORT_OUTCOME_SUCCESS: ClientReportOutcome
CLIENT_REPORT_OUTCOME_ERROR: ClientReportOutcome
CLIENT_REPORT_OUTCOME_CANCELLED: ClientReportOutcome
CLIENT_REPORT_ERROR_UNKNOWN: ClientReportError
CLIENT_REPORT_ERROR_REMOTE_EXECUTION: ClientReportError
CLIENT_REPORT_ERROR_EMPTY_RESULT: ClientReportError
CLIENT_REPORT_ERROR_SERIALIZATION: ClientReportError
CLIENT_REPORT_ERROR_TIMEOUT: ClientReportError
CLIENT_REPORT_ERROR_AUTHORIZATION: ClientReportError
CLIENT_REPORT_ERROR_INVALID_REQUEST: ClientReportError
CLIENT_REPORT_ERROR_RESOURCE_EXHAUSTED: ClientReportError
CLIENT_REPORT_ERROR_SESSION_EXPIRED: ClientReportError
CLIENT_REPORT_ERROR_UNAVAILABLE: ClientReportError
CLIENT_REPORT_ERROR_TRANSPORT: ClientReportError
CLIENT_REPORT_ERROR_CLIENT: ClientReportError

class ExecutionConfig(_message.Message):
    __slots__ = ("imports", "budgets", "gpu", "disable_optimized_execution", "client_reports")
    IMPORTS_FIELD_NUMBER: _ClassVar[int]
    BUDGETS_FIELD_NUMBER: _ClassVar[int]
    GPU_FIELD_NUMBER: _ClassVar[int]
    DISABLE_OPTIMIZED_EXECUTION_FIELD_NUMBER: _ClassVar[int]
    CLIENT_REPORTS_FIELD_NUMBER: _ClassVar[int]
    imports: _containers.RepeatedScalarFieldContainer[str]
    budgets: _containers.RepeatedCompositeFieldContainer[_resources_pb2.ResourceBudget]
    gpu: bool
    disable_optimized_execution: bool
    client_reports: bool
    def __init__(self, imports: _Optional[_Iterable[str]] = ..., budgets: _Optional[_Iterable[_Union[_resources_pb2.ResourceBudget, _Mapping]]] = ..., gpu: _Optional[bool] = ..., disable_optimized_execution: _Optional[bool] = ..., client_reports: _Optional[bool] = ...) -> None: ...

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

class SubmitClientReportRequest(_message.Message):
    __slots__ = ("token", "start_time_unix_ns", "end_time_unix_ns", "duration_ns", "outcome", "error")
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    START_TIME_UNIX_NS_FIELD_NUMBER: _ClassVar[int]
    END_TIME_UNIX_NS_FIELD_NUMBER: _ClassVar[int]
    DURATION_NS_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    token: str
    start_time_unix_ns: int
    end_time_unix_ns: int
    duration_ns: int
    outcome: ClientReportOutcome
    error: ClientReportError
    def __init__(self, token: _Optional[str] = ..., start_time_unix_ns: _Optional[int] = ..., end_time_unix_ns: _Optional[int] = ..., duration_ns: _Optional[int] = ..., outcome: _Optional[_Union[ClientReportOutcome, str]] = ..., error: _Optional[_Union[ClientReportError, str]] = ...) -> None: ...

class SubmitClientReportResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
