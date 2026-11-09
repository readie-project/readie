"""Opt-in, bounded completion reports; never part of execution correctness."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Protocol

import grpc

from readie import errors
from readie._channels import SyncChannelCache
from readie._proto import proxy_pb2, proxy_pb2_grpc
from readie.config import Settings
from readie.protocol import CallRef

METADATA_KEY = "readie-client-report-token"
QUEUE_CAPACITY = 128
TOKEN_LENGTH = 43
UPLOAD_TIMEOUT = 1.0
CLOSE_TIMEOUT = 1.0
RETRY_DELAY = 0.1
SEND_REPORT_ATTEMPTS = 2


class ReportClock(Protocol):
    """A clock with wall time for timestamps and monotonic time for durations."""

    def wall_ns(self) -> int:
        """Return the current unix timestamp in nanoseconds."""
        ...

    def monotonic_ns(self) -> int:
        """Return monotonic time elapsed since some point in time in nanoseconds."""
        ...


class SystemReportClock:
    """A `ReportClock` using the system clock. Used in production."""

    def wall_ns(self) -> int:
        """Return the current Unix timestamp in nanoseconds."""
        return time.time_ns()

    def monotonic_ns(self) -> int:
        """Return the current monotonic clock reading in nanoseconds."""
        return time.perf_counter_ns()


class ReportSender(Protocol):
    """A blocking report uploader used by the background worker."""

    def send(self, report: proxy_pb2.SubmitClientReportRequest) -> None:
        """Try to upload a report."""
        ...

    def close(self) -> None:
        """Cancel any active upload and release the channel."""
        ...


class GrpcReportSender:
    """A `ReportSender` which sends reports to the router's `ProxyService`. Used in production."""

    def __init__(self, settings: Settings) -> None:
        self._channel_cache = SyncChannelCache(settings)
        self._metadata = (
            [("authorization", f"Bearer {settings.auth_token}")] if settings.auth_token else None
        )
        self._lock = threading.Lock()
        self._closed = False
        self._active: grpc.Future | None = None

    def send(self, report: proxy_pb2.SubmitClientReportRequest) -> None:
        """Upload a report."""
        with self._lock:
            if self._closed:
                return
            stub = proxy_pb2_grpc.ProxyServiceStub(self._channel_cache.get())
            call = stub.SubmitClientReport.future(
                report, timeout=UPLOAD_TIMEOUT, metadata=self._metadata
            )
            self._active = call
        try:
            call.result()
        finally:
            with self._lock:
                self._active = None

    def close(self) -> None:
        """Prevent new uploads and interrupt an active one."""
        with self._lock:
            self._closed = True
            if self._active is not None:
                self._active.cancel()
        self._channel_cache.close()


@dataclass(slots=True)
class _CallInfo:
    start_wall_ns: int
    start_mono_ns: int
    request_id: str = ""
    report_token: str = ""


@dataclass(frozen=True, slots=True)
class _CallCompletion:
    report_token: str
    start_time_unix_ns: int
    end_time_unix_ns: int
    duration_ns: int
    outcome: proxy_pb2.ClientReportOutcome
    error: proxy_pb2.ClientReportError

    def message(self) -> proxy_pb2.SubmitClientReportRequest:
        return proxy_pb2.SubmitClientReportRequest(
            token=self.report_token,
            start_time_unix_ns=self.start_time_unix_ns,
            end_time_unix_ns=self.end_time_unix_ns,
            duration_ns=self.duration_ns,
            outcome=self.outcome,
            error=self.error,
        )


class ClientReporter:
    """Times calls and sends them to the router in the background."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: ReportClock | None = None,
        sender: ReportSender | None = None,
    ) -> None:
        self._enabled = settings.client_reports
        self._clock = clock or SystemReportClock()
        self._sender = sender or GrpcReportSender(settings)
        self._lock = threading.Lock()
        self._requests: dict[str, _CallInfo] = {}
        self._completion_queue: queue.Queue[_CallCompletion] = queue.Queue(maxsize=QUEUE_CAPACITY)
        self._thread: threading.Thread | None = None
        self._closing = threading.Event()
        self._abort = threading.Event()

    @contextmanager
    def time_call(self) -> Generator[_CallInfo | None]:
        """Measure an execution request (AKA call)."""
        call_info = None
        if self._enabled:
            with suppress(Exception):
                call_info = _CallInfo(
                    start_wall_ns=self._clock.wall_ns(), start_mono_ns=self._clock.monotonic_ns()
                )
        failure: BaseException | None = None
        try:
            yield call_info
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if call_info is not None:
                with suppress(Exception):
                    self._finish_timing(call_info, failure)

    def register_call(self, ref: CallRef, call_info: _CallInfo | None) -> None:
        """Register a call's information after it's been started."""
        if call_info is not None:
            with self._lock:
                if not self._closing.is_set():
                    call_info.request_id = ref.request_id
                    self._requests[ref.request_id] = call_info

    def register_report_token(self, ref: CallRef, report_token: str) -> None:
        """Attach a report authorization token to a registered call."""
        if len(report_token) != TOKEN_LENGTH or any(
            ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for ch in report_token
        ):
            return
        with self._lock:
            call_info = self._requests.get(ref.request_id)
            if call_info is not None:
                call_info.report_token = report_token

    def close(self) -> None:
        """Drain for at most `CLOSE_TIMEOUT` seconds, then cancel and discard pending work."""
        with self._lock:
            self._closing.set()
            thread = self._thread

        if thread is not None:
            thread.join(timeout=CLOSE_TIMEOUT)

        self._abort.set()

        with suppress(Exception):
            self._sender.close()

        while True:
            try:
                self._completion_queue.get_nowait()
            except queue.Empty:
                break

    def _finish_timing(self, call_info: _CallInfo, failure: BaseException | None) -> None:
        """Finish timing a call by getting finish times and queueing the report to be sent."""
        # Get timings immediately, before any other work, to be as accurate as
        # possible. The try-finally ensures that requests are always unregistered.
        try:
            end_wall_ns = self._clock.wall_ns()
            duration_ns = self._clock.monotonic_ns() - call_info.start_mono_ns
        finally:
            with self._lock:
                self._requests.pop(call_info.request_id, None)

        if not call_info.report_token:
            return

        outcome, error = to_report_result_pair(failure)
        completion = _CallCompletion(
            report_token=call_info.report_token,
            start_time_unix_ns=call_info.start_wall_ns,
            end_time_unix_ns=end_wall_ns,
            duration_ns=duration_ns,
            outcome=outcome,
            error=error,
        )
        with self._lock:
            if self._closing.is_set():
                return

            try:
                self._completion_queue.put_nowait(completion)
            except queue.Full:
                return

            if self._thread is None:
                thread = threading.Thread(
                    target=self._send_reports, daemon=True, name="readie-report-sender"
                )
                thread.start()
                self._thread = thread

    def _send_reports(self) -> None:
        """Pull from the completion queue until aborted, sending reports from completions."""
        while not self._abort.is_set():
            try:
                completion = self._completion_queue.get(timeout=0.05)
            except queue.Empty:
                if self._closing.is_set():
                    return
                continue

            for attempt in range(SEND_REPORT_ATTEMPTS):
                if self._abort.is_set():
                    return

                try:
                    self._sender.send(completion.message())
                    break
                except grpc.RpcError as exc:
                    if (
                        attempt != 0
                        or exc.code()
                        not in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED)
                        or self._abort.wait(RETRY_DELAY)
                    ):
                        break
                except Exception:
                    break


def to_report_result_pair(
    failure: BaseException | None,
) -> tuple[proxy_pb2.ClientReportOutcome, proxy_pb2.ClientReportError]:
    """Convert a Python exception to a client report outcome and error pair."""
    if failure is None:
        return proxy_pb2.CLIENT_REPORT_OUTCOME_SUCCESS, proxy_pb2.CLIENT_REPORT_ERROR_UNKNOWN
    if isinstance(
        failure,
        (asyncio.CancelledError, errors.ExecutionCancelledError, KeyboardInterrupt, SystemExit),
    ):
        return proxy_pb2.CLIENT_REPORT_OUTCOME_CANCELLED, proxy_pb2.CLIENT_REPORT_ERROR_UNKNOWN

    category = proxy_pb2.CLIENT_REPORT_ERROR_CLIENT
    if isinstance(failure, errors.RemoteExecutionError):
        category = proxy_pb2.CLIENT_REPORT_ERROR_REMOTE_EXECUTION
    elif isinstance(failure, errors.EmptyResultError):
        category = proxy_pb2.CLIENT_REPORT_ERROR_EMPTY_RESULT
    elif isinstance(failure, errors.SerializationError):
        category = proxy_pb2.CLIENT_REPORT_ERROR_SERIALIZATION
    elif isinstance(failure, errors.RemoteTimeoutError):
        category = proxy_pb2.CLIENT_REPORT_ERROR_TIMEOUT
    elif isinstance(failure, errors.PermissionDeniedError):
        category = proxy_pb2.CLIENT_REPORT_ERROR_AUTHORIZATION
    elif isinstance(failure, errors.InvalidRequestError):
        category = proxy_pb2.CLIENT_REPORT_ERROR_INVALID_REQUEST
    elif isinstance(failure, errors.ResourceExhaustedError):
        category = proxy_pb2.CLIENT_REPORT_ERROR_RESOURCE_EXHAUSTED
    elif isinstance(failure, errors.SessionExpiredError):
        category = proxy_pb2.CLIENT_REPORT_ERROR_SESSION_EXPIRED
    elif isinstance(failure, errors.ClusterUnavailableError):
        category = proxy_pb2.CLIENT_REPORT_ERROR_UNAVAILABLE
    elif isinstance(failure, errors.TransportError):
        category = proxy_pb2.CLIENT_REPORT_ERROR_TRANSPORT

    return proxy_pb2.CLIENT_REPORT_OUTCOME_ERROR, category


def get_report_token(metadata: object) -> str | None:
    """Extract the report token from metadata, suppressing exceptions."""
    with suppress(Exception):
        for key, value in metadata:  # type: ignore[attr-defined]
            if key == METADATA_KEY and isinstance(value, str):
                return value
    return None
