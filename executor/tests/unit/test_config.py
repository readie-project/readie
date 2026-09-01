"""Settings parsing."""

from __future__ import annotations

import pytest

from readie_executor.config import (
    DEFAULT_CHUNK_SIZE,
    ConfigError,
    Settings,
    parse_preimport,
)


def test_executor_dir_is_required_and_explained():
    # The previous executor exited with a bare message naming the variable but
    # not what it is for.
    with pytest.raises(ConfigError, match="binds its socket"):
        Settings.from_env({})


def test_a_blank_executor_dir_is_also_missing():
    with pytest.raises(ConfigError, match="EXECUTOR_DIR"):
        Settings.from_env({"EXECUTOR_DIR": "   "})


def test_defaults():
    settings = Settings.from_env({"EXECUTOR_DIR": "/tmp"})
    assert settings.socket_path == "/tmp/executor.sock"
    assert settings.chunk_size == DEFAULT_CHUNK_SIZE
    assert settings.preimport == ()


def test_overrides():
    settings = Settings.from_env(
        {
            "EXECUTOR_DIR": "/run",
            "EXECUTOR_SOCKET_NAME": "other.sock",
            "EXECUTOR_CHUNK_SIZE": "4096",
            "EXECUTOR_MODE": "capture",
            "READIE_PREIMPORT": "pandas,numpy",
        }
    )
    assert settings.socket_path == "/run/other.sock"
    assert settings.chunk_size == 4096
    assert settings.preimport == ("pandas", "numpy")


def test_settings_are_immutable():
    settings = Settings.from_env({"EXECUTOR_DIR": "/tmp"})
    with pytest.raises(AttributeError):
        settings.chunk_size = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "env",
    [
        {"EXECUTOR_CHUNK_SIZE": "lots"},
        {"EXECUTOR_CHUNK_SIZE": "0"},
        {"EXECUTOR_CHUNK_SIZE": "-1"},
    ],
)
def test_unusable_values_are_rejected(env):
    with pytest.raises(ConfigError):
        Settings.from_env({"EXECUTOR_DIR": "/tmp", **env})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        ("   ", ()),
        ("pandas", ("pandas",)),
        ("pandas,numpy", ("pandas", "numpy")),
        (" pandas , numpy ", ("pandas", "numpy")),
        ("pandas,,numpy,", ("pandas", "numpy")),
        ("pandas,numpy,pandas", ("pandas", "numpy")),
    ],
)
def test_preimport_parsing(raw, expected):
    assert parse_preimport(raw) == expected


def test_preimport_preserves_order_rather_than_sorting():
    # Listing numpy before pandas may express a dependency, and reordering
    # imports changes which module initialises first.
    assert parse_preimport("scipy,numpy,pandas") == ("scipy", "numpy", "pandas")
