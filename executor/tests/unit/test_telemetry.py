"""Fixed phase measurements use a fresh monotonic anchor per request."""

import json

from readie_executor.telemetry import RequestTimings


class Clock:
    def __init__(self):
        self.tick = 0

    def time_ns(self):
        return 1_700_000_000_000_000_000 + self.tick

    def perf_counter_ns(self):
        self.tick += 10
        return self.tick


def test_phases_use_monotonic_offsets_and_do_not_include_telemetry_encoding():
    clock = Clock()
    timings = RequestTimings(clock)
    with timings.phase("receive"):
        pass
    with timings.phase("decode"):
        pass
    data = json.loads(timings.serialize())
    assert data == {
        "start_time_unix_ns": 1_700_000_000_000_000_010,
        "duration_ns": 40,
        "outcome": "success",
        "phases": [
            {"phase": "receive", "start_offset_ns": 10, "duration_ns": 10, "outcome": "success"},
            {"phase": "decode", "start_offset_ns": 30, "duration_ns": 10, "outcome": "success"},
        ],
    }
    clock.perf_counter_ns()  # Delayed collection does not inflate execution time.
    assert json.loads(timings.serialize()) == data
    again = RequestTimings(clock)
    assert json.loads(again.serialize())["start_time_unix_ns"] > data["start_time_unix_ns"]
