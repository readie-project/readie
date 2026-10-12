import itertools

import pytest

from readie_pipeline.corpus.sources.pacing import Pacer, http_status


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(str(status))
        self.response = type("Response", (), {"status_code": status})()


class Clock:
    """A clock that only moves when the pacer sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make(**overrides):
    clock = Clock()
    lines: list[str] = []
    pacer = Pacer(sleep=clock.sleep, clock=clock, on_line=lines.append, **overrides)
    return clock, lines, pacer


def failing(status, times):
    """A call that raises ``status`` the first ``times`` times, then returns "ok"."""
    state = {"left": times}

    def call():
        if state["left"] > 0:
            state["left"] -= 1
            raise HttpError(status)
        return "ok"

    return call


def test_calls_are_spaced_by_the_interval():
    clock, _, pacer = make(base_interval=1.0)
    pacer.call(lambda: None)
    pacer.call(lambda: None)
    pacer.call(lambda: None)
    assert clock.sleeps == [1.0, 1.0]


def test_a_call_that_took_longer_than_the_interval_is_not_delayed_further():
    clock, _, pacer = make(base_interval=1.0)

    def slow():
        clock.now += 5.0

    pacer.call(slow)
    pacer.call(lambda: None)
    assert clock.sleeps == []


def test_a_429_widens_the_spacing_up_to_a_ceiling():
    _, _, pacer = make(base_interval=0.5, max_interval=4.0, attempts=10)
    pacer.call(failing(429, 5))
    assert pacer.interval <= 4.0
    # 0.5 doubled five times would be 16; the ceiling holds it at 4, and the
    # success that ended the run eased it down by a twentieth.
    assert pacer.interval == pytest.approx(4.0 * 0.95)


def test_the_spacing_eases_back_to_the_base_while_calls_succeed():
    _, _, pacer = make(base_interval=0.3, max_interval=30.0)
    pacer.call(failing(429, 4))
    widened = pacer.interval
    for _ in range(200):
        pacer.call(lambda: None)
    assert widened > 1.0
    assert pacer.interval == pytest.approx(0.3)


def test_a_server_error_is_retried_but_does_not_widen_the_spacing():
    _, _, pacer = make(base_interval=0.3)
    assert pacer.call(failing(503, 2)) == "ok"
    assert pacer.interval == pytest.approx(0.3)


def test_waits_double_from_the_base_up_to_the_cap():
    clock, _, pacer = make(base_interval=0.0, base_backoff=5.0, max_backoff=40.0, attempts=8)
    pacer.call(failing(429, 6))
    assert clock.sleeps == [5.0, 10.0, 20.0, 40.0, 40.0, 40.0]


def test_a_call_that_stays_refused_raises_after_the_attempts_are_used():
    clock, _, pacer = make(base_interval=0.0, attempts=4)
    with pytest.raises(HttpError):
        pacer.call(failing(429, 99))
    assert len(clock.sleeps) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_status_that_will_not_improve_is_raised_at_once(status):
    clock, _, pacer = make(base_interval=0.0)
    with pytest.raises(HttpError):
        pacer.call(failing(status, 1))
    assert clock.sleeps == []


def test_an_error_without_a_status_is_raised_at_once():
    clock, _, pacer = make(base_interval=0.0)
    with pytest.raises(ConnectionError):
        pacer.call(lambda: (_ for _ in ()).throw(ConnectionError("reset")))
    assert clock.sleeps == []


def test_a_long_wait_is_reported_so_a_stall_is_visible():
    # Seen live: a pair sat idle for 40 minutes and nothing said why.
    _, lines, pacer = make(base_interval=0.0, base_backoff=5.0)
    pacer.call(failing(429, 4))
    assert any("answered 429; waiting 20s" in line for line in lines)
    assert not any("waiting 5s" in line for line in lines)


def test_the_status_is_read_from_the_response_the_way_requests_attaches_it():
    assert http_status(HttpError(429)) == 429
    assert http_status(ValueError("x")) is None


def test_calls_from_several_threads_are_spread_by_the_interval_not_released_together():
    # Downloads run in groups, so the pacer is called concurrently and must still
    # hold the request rate, not let a whole group start in the same instant.
    import threading
    import time

    started: list[float] = []
    lock = threading.Lock()

    def record():
        with lock:
            started.append(time.monotonic())

    pacer = Pacer(base_interval=0.05)
    threads = [threading.Thread(target=pacer.call, args=(record,)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    started.sort()
    gaps = [b - a for a, b in itertools.pairwise(started)]
    assert min(gaps) >= 0.04  # a little under 0.05 to allow for timer granularity


def test_the_reserved_slots_do_not_drift_when_calls_arrive_together():
    clock, _, pacer = make(base_interval=1.0)
    for _ in range(4):
        pacer.call(lambda: None)
    assert clock.sleeps == [1.0, 1.0, 1.0]
