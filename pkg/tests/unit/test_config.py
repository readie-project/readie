"""Settings validation and environment parsing."""

from __future__ import annotations

from typing import Any

import pytest

from readie.config import DEFAULT_ROUTER_URI, Settings
from readie.errors import ConfigurationError


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
