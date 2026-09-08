"""Pipeline settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from readie_pipeline.config import ConfigError, CorpusSettings, Settings


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "bundle_dir": Path("/app/executorfs"),
        "output_dir": Path("/app/executor"),
        "data_dir": Path("/data"),
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def test_derived_paths():
    s = settings()
    assert s.rootfs_path == Path("/app/executorfs/rootfs")
    assert s.checkpoints_dir == Path("/app/executor/checkpoints")
    assert s.plan_path == Path("/app/executor/checkpoints.json")
    assert s.corpus_path == Path("/data/dataset.json")


def test_a_plan_dir_moves_the_plan_off_the_output_directory():
    # The worker image COPYs output_dir wholesale, and the plan is an input to a
    # capture rather than something the worker ships beside its manifest.
    s = settings(plan_dir=Path("/app/plan"))
    assert s.plan_path == Path("/app/plan/checkpoints.json")
    assert s.fingerprint_path == Path("/app/plan/spec-fingerprint.txt")
    assert s.checkpoints_dir == Path("/app/executor/checkpoints"), "outputs do not move"


def test_the_socket_dir_is_a_host_path_distinct_from_the_sandbox_one():
    # Conflating the two once sent the pre-imported executor source to the wrong
    # place, so every checkpoint captured a bare executor.
    assert settings().socket_dir == Path("/app/executor/socket")


def test_global_runsc_flags_precede_the_subcommand_and_match_the_worker():
    flags = settings().global_runsc_flags
    assert flags[0] == "--network=sandbox"
    assert "--host-uds=create" in flags
    assert "--overlay2=root:memory" in flags


def test_an_all_overlay_is_refused():
    # It would keep the executor's socket in the overlay's upper layer, where
    # the worker cannot see it: every execution fails at dial time looking
    # exactly like a dead executor.
    with pytest.raises(ConfigError, match="hide the executor socket"):
        settings(sandbox_overlay="all:memory")


def test_a_self_overlay_is_refused():
    with pytest.raises(ConfigError, match="shared rootfs"):
        settings(sandbox_overlay="root:self")


@pytest.mark.parametrize(
    "override",
    [{"max_checkpoints": 0}, {"checkpoint_size_budget_mb": 0}, {"checkpoint_timeout": 0}],
)
def test_nonsensical_bounds_are_refused(override):
    with pytest.raises(ConfigError):
        settings(**override)


def test_settings_are_immutable():
    with pytest.raises(AttributeError):
        settings().planner = "fixed"  # type: ignore[misc]


def test_from_env_reads_the_image_defaults():
    s = Settings.from_env({})
    assert s.bundle_dir == Path("/app/executorfs")
    assert s.output_dir == Path("/app/executor")
    assert s.planner == "greedy"


def test_explicit_overrides_beat_the_environment():
    # Overrides come from the command line, so they win over what the image
    # baked in.
    s = Settings.from_env({"READIE_PLANNER": "fixed"}, planner="greedy")
    assert s.planner == "greedy"


def test_a_none_override_does_not_erase_an_environment_value():
    # argparse supplies None for every flag the user did not pass.
    s = Settings.from_env({"READIE_PLANNER": "fixed"}, planner=None)
    assert s.planner == "fixed"


def test_an_unparsable_number_is_a_configuration_error():
    with pytest.raises(ConfigError, match="READIE_MAX_CHECKPOINTS"):
        Settings.from_env({"READIE_MAX_CHECKPOINTS": "lots"})


def test_corpus_credentials_are_all_required_and_named():
    # The previous code passed os.environ.get(...) straight through, so an unset
    # AZURE_MODEL_NAME reached the API as model=None.
    with pytest.raises(ConfigError, match="AZURE_ENDPOINT, AZURE_API_KEY, AZURE_MODEL_NAME"):
        CorpusSettings.from_env({})


def test_corpus_credentials_load_when_present():
    s = CorpusSettings.from_env(
        {"AZURE_ENDPOINT": "https://x", "AZURE_API_KEY": "k", "AZURE_MODEL_NAME": "gpt"}
    )
    assert s.model == "gpt"
