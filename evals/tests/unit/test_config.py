from __future__ import annotations

import pytest

from readie_evals.config import ConfigError, Settings


def test_from_env_applies_defaults_when_unset():
    settings = Settings.from_env(env={})
    assert settings.router_uri == "localhost:50051"
    assert settings.tls is False
    assert settings.per_cell == 3
    assert settings.agent_configured is False
    assert settings.concurrency == 10
    assert settings.container_release_wait == 35.0


def test_from_env_reads_concurrency_and_release_wait():
    settings = Settings.from_env(
        env={"READIE_EVALS_CONCURRENCY": "4", "READIE_EVALS_RELEASE_WAIT": "10"},
    )
    assert settings.concurrency == 4
    assert settings.container_release_wait == 10.0


def test_from_env_reads_azure_variables():
    settings = Settings.from_env(
        env={
            "AZURE_API_KEY": "  secret  ",
            "AZURE_ENDPOINT": "https://r.services.ai.azure.com/anthropic/",
            "AZURE_MODEL_NAME": "claude-sonnet-4-5",
            "READIE_EVALS_PER_CELL": "5",
        },
    )
    assert settings.azure_api_key == "secret"
    assert settings.azure_endpoint == "https://r.services.ai.azure.com/anthropic/"
    assert settings.azure_model == "claude-sonnet-4-5"
    assert settings.per_cell == 5
    assert settings.agent_configured is True


def test_require_agent_names_missing_variables():
    with pytest.raises(ConfigError, match="AZURE_ENDPOINT, AZURE_API_KEY, AZURE_MODEL_NAME"):
        Settings.from_env(env={}).require_agent()


def test_require_agent_passes_when_configured():
    settings = Settings.from_env(
        env={"AZURE_API_KEY": "k", "AZURE_ENDPOINT": "e", "AZURE_MODEL_NAME": "m"},
    )
    settings.require_agent()  # does not raise


def test_derived_paths_live_under_the_out_dir(tmp_path):
    settings = Settings.from_env(env={"READIE_EVALS_OUT_DIR": str(tmp_path)})
    assert settings.corpus_path == tmp_path / "corpus.jsonl"
    assert settings.results_path == tmp_path / "results.jsonl"


def test_timeout_can_be_disabled():
    assert Settings.from_env(env={"READIE_EVALS_TIMEOUT": "none"}).call_timeout is None
