"""Settings validation and environment parsing."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

import readie
from readie.config import DEFAULT_ROUTER_URI, Settings
from readie.errors import ConfigurationError


def test_defaults_are_usable() -> None:
    settings = Settings()
    assert settings.router_uri == DEFAULT_ROUTER_URI
    assert settings.timeout is None


def test_an_empty_router_uri_fails_at_construction() -> None:
    with pytest.raises(ConfigurationError, match="router_uri is required"):
        Settings(router_uri="")


def test_the_empty_router_uri_error_does_not_name_an_env_var_nothing_reads() -> None:
    # Pins a past bug: the message told users to set READIE_ROUTER_URI, which no
    # code reads, so following the advice changed nothing.
    with pytest.raises(ConfigurationError) as excinfo:
        Settings(router_uri="")
    assert "READIE_ROUTER_URI" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"timeout": 0.0}, "timeout must be positive"),
        ({"timeout": -3.0}, "timeout must be positive"),
    ],
)
def test_incoherent_settings_are_rejected(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ConfigurationError, match=match):
        Settings(**kwargs)


def test_settings_are_immutable() -> None:
    settings = Settings()
    with pytest.raises(AttributeError):
        settings.router_uri = "elsewhere:1"  # type: ignore[misc]


def test_the_reported_version_is_the_one_in_pyproject() -> None:
    # Pins a past bug: __version__ was a hand-written literal ("0.1.0") that fell
    # behind pyproject.toml (0.3.0). It is now read from the installed metadata.
    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert readie.__version__ == declared
