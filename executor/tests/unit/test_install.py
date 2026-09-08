"""Installing packages with `uv` before a call runs."""

from __future__ import annotations

import subprocess
import sys
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

import pytest

import readie_executor.install as install_mod
from readie_executor.install import InstallError, install_packages


class _FakeDistribution:
    def __init__(self, name: str, version: str) -> None:
        self.metadata = {"Name": name}
        self.version = version


def _versions(monkeypatch: pytest.MonkeyPatch, before: dict[str, str], after: dict[str, str]):
    """Make the pre- and post-install distribution snapshots differ as given."""
    calls = iter(
        [
            [_FakeDistribution(name, version) for name, version in before.items()],
            [_FakeDistribution(name, version) for name, version in after.items()],
        ]
    )
    monkeypatch.setattr(metadata, "distributions", lambda: next(calls))
    monkeypatch.setattr(
        metadata,
        "packages_distributions",
        lambda: {name: [name] for name in {**before, **after}},
    )


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


def test_a_changed_distribution_evicts_its_cached_submodules(monkeypatch: pytest.MonkeyPatch):
    # Stands in for a checkpoint restore that preloaded pandas 2.3.3: the next
    # `import pandas` inside the call must not silently hand back this object.
    monkeypatch.setitem(sys.modules, "pandas", object())
    monkeypatch.setitem(sys.modules, "pandas.core", object())
    _versions(monkeypatch, before={"pandas": "2.3.3"}, after={"pandas": "2.1.3"})

    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="Installed 1 package\n", stderr=""
    )
    with patch("subprocess.run", return_value=completed):
        install_packages(["pandas==2.1.3"])

    assert "pandas" not in sys.modules
    assert "pandas.core" not in sys.modules


def test_an_unrelated_module_survives_the_eviction(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "pandas", object())
    sentinel = object()
    monkeypatch.setitem(sys.modules, "requests", sentinel)
    _versions(monkeypatch, before={"pandas": "2.3.3"}, after={"pandas": "2.1.3"})

    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=completed):
        install_packages(["pandas==2.1.3"])

    assert sys.modules["requests"] is sentinel


def test_an_install_that_changes_nothing_evicts_nothing(monkeypatch: pytest.MonkeyPatch):
    # `uv` finding the pin already satisfied is still a call worth surviving --
    # no version moved, so there is nothing stale to evict.
    sentinel = object()
    monkeypatch.setitem(sys.modules, "pandas", sentinel)
    _versions(monkeypatch, before={"pandas": "2.1.3"}, after={"pandas": "2.1.3"})

    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=completed):
        install_packages(["pandas==2.1.3"])

    assert sys.modules["pandas"] is sentinel
