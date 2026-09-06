"""Installing packages with `uv` before a call runs."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from readie_executor.install import InstallError, install_packages


def test_an_empty_list_runs_no_subprocess():
    with patch("subprocess.run") as run:
        assert install_packages([]) == ""
    run.assert_not_called()


def test_a_successful_install_returns_the_captured_output():
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="Installed 1 package\n", stderr=""
    )
    with patch("subprocess.run", return_value=completed) as run:
        output = install_packages(["numpy"])

    assert output == "Installed 1 package\n"
    args = run.call_args.args[0]
    assert args[:5] == ["uv", "pip", "install", "--system", "--no-cache-dir"]
    assert args[5:] == ["numpy"]


def test_every_spec_is_passed_to_uv():
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=completed) as run:
        install_packages(["numpy", "requests==2.31.0"])

    args = run.call_args.args[0]
    assert args[-2:] == ["numpy", "requests==2.31.0"]


def test_a_nonzero_exit_raises_with_the_captured_output():
    completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="ERROR: No matching distribution found\n"
    )
    with (
        patch("subprocess.run", return_value=completed),
        pytest.raises(InstallError, match="No matching distribution"),
    ):
        install_packages(["not-a-real-package"])


def test_the_error_names_the_exit_code():
    completed = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="boom")
    with (
        patch("subprocess.run", return_value=completed),
        pytest.raises(InstallError, match="exit 2"),
    ):
        install_packages(["x"])


def test_a_custom_uv_binary_is_honoured():
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=completed) as run:
        install_packages(["numpy"], uv_binary="/opt/uv")

    assert run.call_args.args[0][0] == "/opt/uv"
