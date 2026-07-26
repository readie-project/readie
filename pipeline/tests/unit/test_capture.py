"""Capturing a checkpoint, without runsc."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import pytest

from crfs_pipeline.capture.build import READY_SENTINEL, _await_ready


def spawn(script: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-u", "-c", textwrap.dedent(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def drive(script: str, *, timeout: float) -> tuple[bool, list[str], float]:
    lines: list[str] = []
    started = time.monotonic()

    # `with` closes the stdout pipe, mirroring what capture() does.
    with spawn(script) as process:
        try:
            ready = _await_ready(process, timeout=timeout, on_line=lines.append)
        finally:
            if process.poll() is None:
                process.kill()

    return ready, lines, time.monotonic() - started


def test_the_sentinel_is_detected():
    ready, lines, _ = drive(f"print('warming up'); print('{READY_SENTINEL}')", timeout=10)

    assert ready
    assert "warming up" in lines


def test_output_before_the_sentinel_is_reported():
    # It is the only window into what the sandbox is doing, and a build that
    # fails here fails for minutes.
    ready, lines, _ = drive(
        f"""
        for i in range(3):
            print(f'step {{i}}')
        print('{READY_SENTINEL}')
        """,
        timeout=10,
    )

    assert ready
    assert lines[:3] == ["step 0", "step 1", "step 2"]


def test_a_sandbox_that_exits_without_announcing_is_not_ready():
    ready, _, elapsed = drive("print('starting'); raise SystemExit(1)", timeout=10)

    assert not ready
    assert elapsed < 5, "it must notice the exit rather than wait out the timeout"


def test_a_silent_sandbox_times_out():
    # The defect this replaced: the deadline used to be checked *inside* the
    # readline loop, so a sandbox producing no output at all blocked in
    # readline forever and the timeout never fired -- in exactly the case it
    # was written to bound.
    ready, _, elapsed = drive("import time; time.sleep(60)", timeout=0.5)

    assert not ready
    assert elapsed < 5, "the timeout must fire while no output is arriving"


def test_a_sandbox_that_talks_but_never_announces_also_times_out():
    ready, lines, elapsed = drive(
        """
        import time
        while True:
            print('still working')
            time.sleep(0.05)
        """,
        timeout=0.5,
    )

    assert not ready
    assert lines, "its output was still reported"
    assert elapsed < 5


def test_a_slow_but_eventually_ready_sandbox_succeeds():
    ready, _, _ = drive(
        f"""
        import time
        time.sleep(0.3)
        print('{READY_SENTINEL}')
        """,
        timeout=10,
    )
    assert ready


@pytest.mark.parametrize("suffix", ["", " with trailing text"])
def test_the_sentinel_is_matched_anywhere_in_the_line(suffix):
    ready, _, _ = drive(f"print('{READY_SENTINEL}{suffix}')", timeout=10)
    assert ready
