from readie_playground.limits import RateLimiter, RunSlots


def test_rate_limiter_blocks_over_the_limit_then_recovers() -> None:
    now = [0.0]
    limiter = RateLimiter(2, 10.0, clock=lambda: now[0])
    assert limiter.allow("a")
    assert limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")
    now[0] = 11.0
    assert limiter.allow("a")


def test_run_slots_reject_when_full_and_release() -> None:
    slots = RunSlots(1)
    assert slots.try_acquire()
    assert not slots.try_acquire()
    slots.release()
    assert slots.try_acquire()
