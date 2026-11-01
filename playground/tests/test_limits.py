from readie_playground.limits import RunSlots


def test_run_slots_reject_when_full_and_release() -> None:
    slots = RunSlots(1)
    assert slots.try_acquire()
    assert not slots.try_acquire()
    slots.release()
    assert slots.try_acquire()
