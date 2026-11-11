"""Bounded, dependency-free execution timings sent after the result."""

from __future__ import annotations

import json
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Protocol

MAX_TELEMETRY_BYTES = 8192
TELEMETRY_TIMEOUT = 0.1


# TODO: There is an essentially identical protocol in the client's _reports.py
# There may be similar opportunities for reducing shared code. Investigate and
# perhaps make a shared utilities module.
class Clock(Protocol):
    """Wall-clock placement and monotonic elapsed time."""

    def time_ns(self) -> int:
        """Return Unix epoch nanoseconds."""
        ...

    def perf_counter_ns(self) -> int:
        """Return monotonic nanoseconds."""
        ...


class RequestTimings:
    """Collect one request's fixed phases without serializing on the result path."""

    def __init__(self, clock: Clock = time) -> None:
        self._clock = clock
        self._start_mono = clock.perf_counter_ns()
        self._end_mono = self._start_mono
        self._start_wall = clock.time_ns()
        self._outcome = "success"
        self._phases: list[dict[str, str | int]] = []

    def fail(self, outcome: str) -> None:
        """Register a failure, setting the overall and last phase's outcome."""
        self._outcome = outcome
        self._phases[-1]["outcome"] = outcome

    @contextmanager
    def phase(self, name: str) -> Generator[None]:
        """Measure a phase, including exceptions caught by its caller."""
        start = self._clock.perf_counter_ns()
        record: dict[str, str | int] = {
            "phase": name,
            "start_offset_ns": start - self._start_mono,
            "duration_ns": 0,
            "outcome": "success",
        }
        self._phases.append(record)
        try:
            yield
        except BaseException:
            self.fail(f"{name}_error")
            raise
        finally:
            self._end_mono = self._clock.perf_counter_ns()
            record["duration_ns"] = self._end_mono - start

    def serialize(self) -> bytes:
        """Serialize the timings to JSON bytes.

        Should only be called after the result has finished being transmitted so
        latency is not affected.
        """
        data = json.dumps(
            {
                "start_time_unix_ns": self._start_wall,
                "duration_ns": self._end_mono - self._start_mono,
                "outcome": self._outcome,
                "phases": self._phases,
            },
            separators=(",", ":"),
        ).encode()
        if len(data) > MAX_TELEMETRY_BYTES:
            msg = "executor telemetry exceeds its size limit"
            raise ValueError(msg)
        return data
