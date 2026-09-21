"""Bootstrapping the sandbox's network before a call needs it."""

from __future__ import annotations

from pathlib import Path

import pytest

import readie_executor.network as network_mod
from readie_executor.network import ensure_dns


def _point_resolv_conf_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    resolv = tmp_path / "resolv.conf"
    default = tmp_path / "readie-resolv.conf"
    monkeypatch.setattr(network_mod, "_RESOLV_CONF", resolv)
    monkeypatch.setattr(network_mod, "_RESOLV_CONF_DEFAULT", default)
    return resolv, default


def test_an_empty_resolv_conf_is_replaced_with_the_baked_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    resolv, default = _point_resolv_conf_at(monkeypatch, tmp_path)
    resolv.write_text("")
    default.write_text("nameserver 8.8.8.8\n")

    ensure_dns()

    assert resolv.read_text() == "nameserver 8.8.8.8\n"


def test_a_populated_resolv_conf_is_left_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    resolv, default = _point_resolv_conf_at(monkeypatch, tmp_path)
    resolv.write_text("nameserver 127.0.0.11\n")
    default.write_text("nameserver 8.8.8.8\n")

    ensure_dns()

    assert resolv.read_text() == "nameserver 127.0.0.11\n"


def test_a_missing_baked_default_is_a_safe_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # An older rootfs built before the fix has no baked default to copy from;
    # this must not fail the call over it.
    resolv, _default = _point_resolv_conf_at(monkeypatch, tmp_path)
    resolv.write_text("")

    ensure_dns()  # must not raise

    assert resolv.read_text() == ""
