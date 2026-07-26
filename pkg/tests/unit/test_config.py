"""Settings validation and environment parsing."""

from __future__ import annotations

from typing import Any

import pytest

from crfs.config import DEFAULT_ROUTER_URI, Settings
from crfs.errors import ConfigurationError


def test_defaults_are_usable() -> None:
    settings = Settings()
    assert settings.router_uri == DEFAULT_ROUTER_URI
    assert settings.timeout is None


def test_an_empty_router_uri_fails_at_construction() -> None:
    # The old client passed os.environ.get("ROUTER_URI") -- possibly None --
    # straight into insecure_channel, so a missing variable surfaced far away.
    with pytest.raises(ConfigurationError, match="router_uri is required"):
        Settings(router_uri="")


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"chunk_size": 0}, "chunk_size must be positive"),
        ({"chunk_size": -1}, "chunk_size must be positive"),
        ({"max_message_bytes": 512, "chunk_size": 1024}, "must exceed"),
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


def test_from_env_reads_the_prefixed_names() -> None:
    settings = Settings.from_env(
        {"CRFS_ROUTER_URI": "router:50051", "CRFS_TIMEOUT": "12.5", "CRFS_STREAM_LOGS": "no"}
    )
    assert settings.router_uri == "router:50051"
    assert settings.timeout == 12.5
    assert settings.stream_logs is False


def test_from_env_still_accepts_the_old_unprefixed_name() -> None:
    assert Settings.from_env({"ROUTER_URI": "old:1"}).router_uri == "old:1"


def test_the_prefixed_name_wins_over_the_old_one() -> None:
    env = {"ROUTER_URI": "old:1", "CRFS_ROUTER_URI": "new:2"}
    assert Settings.from_env(env).router_uri == "new:2"


def test_from_env_falls_back_when_a_variable_is_blank() -> None:
    assert Settings.from_env({"CRFS_ROUTER_URI": "", "CRFS_TIMEOUT": "  "}).timeout is None


def test_an_unparsable_number_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="CRFS_TIMEOUT"):
        Settings.from_env({"CRFS_TIMEOUT": "soon"})
