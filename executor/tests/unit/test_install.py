"""Installing packages with `uv` before a call runs."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import readie_executor.install as install_mod
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


def _point_resolv_conf_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    resolv = tmp_path / "resolv.conf"
    default = tmp_path / "readie-resolv.conf"
    monkeypatch.setattr(install_mod, "_RESOLV_CONF", resolv)
    monkeypatch.setattr(install_mod, "_RESOLV_CONF_DEFAULT", default)
    return resolv, default


def test_an_empty_resolv_conf_is_replaced_with_the_baked_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    resolv, default = _point_resolv_conf_at(monkeypatch, tmp_path)
    resolv.write_text("")
    default.write_text("nameserver 8.8.8.8\n")

    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=completed):
        install_packages(["numpy"])

    assert resolv.read_text() == "nameserver 8.8.8.8\n"


def test_a_populated_resolv_conf_is_left_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    resolv, default = _point_resolv_conf_at(monkeypatch, tmp_path)
    resolv.write_text("nameserver 127.0.0.11\n")
    default.write_text("nameserver 8.8.8.8\n")

    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=completed):
        install_packages(["numpy"])

    assert resolv.read_text() == "nameserver 127.0.0.11\n"


def test_a_missing_baked_default_is_a_safe_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # An older rootfs built before the fix has no baked default to copy from;
    # this must not fail the call over it.
    resolv, _default = _point_resolv_conf_at(monkeypatch, tmp_path)
    resolv.write_text("")

    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=completed):
        install_packages(["numpy"])  # must not raise

    assert resolv.read_text() == ""
