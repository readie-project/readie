"""The manifests the worker reads."""

from __future__ import annotations

import json
from pathlib import Path

from crfs_pipeline.config import EXECUTOR_PROTOCOL
from crfs_pipeline.manifest import CheckpointMeta, Generation, write_plan


def generation(**overrides: object) -> Generation:
    base: dict[str, object] = {
        "id": "gen-1",
        "rootfs_id": "sha256:rootfs",
        "runsc_version": "runsc version release-20260721.0",
        "spec_fingerprint": "sha256:spec",
        "python_path": "/lib/python3.12/dist-packages",
        "overlay": "root:memory",
        "network": "none",
    }
    return Generation(**{**base, **overrides})  # type: ignore[arg-type]


def test_the_manifest_records_argv_not_a_path():
    # The executor is an installed wheel, so there is no stable source file to
    # point at. The worker replays this verbatim rather than assembling its own.
    payload = generation().to_json()
    assert payload["executor_argv"] == ["python", "-u", "-m", "crfs_executor"]
    assert "executor_entrypoint" not in payload


def test_the_manifest_records_the_protocol_version():
    # The worker refuses a generation whose executor speaks a format it does not
    # implement. Without this field it would default to 1 and be refused.
    assert generation().to_json()["executor_protocol"] == EXECUTOR_PROTOCOL


def test_every_field_the_go_reader_requires_is_present():
    # worker/internal/artifact.Generation.Validate insists on these.
    payload = generation().to_json()
    for name in ("id", "rootfs_id", "runsc_version", "spec_fingerprint", "executor_argv"):
        assert payload[name], name


def test_the_timestamp_is_rfc3339_utc():
    # time.Time in Go parses this; a local-time stamp would fail to unmarshal.
    stamp = generation().to_json()["created_at"]
    assert stamp.endswith("Z")
    assert len(stamp) == len("2026-01-01T00:00:00Z")


def test_writing_is_atomic(tmp_path: Path):
    # A manifest half-written by an interrupted build is worse than none: the
    # worker would load it and refuse the whole generation for a reason that has
    # nothing to do with the checkpoints.
    generation().write(tmp_path)

    assert (tmp_path / "generation.json").is_file()
    assert list(tmp_path.glob("*.partial")) == []


def test_a_written_manifest_reads_back_as_json(tmp_path: Path):
    path = generation().write(tmp_path)
    assert json.loads(path.read_text())["id"] == "gen-1"


def test_checkpoint_meta_is_self_describing(tmp_path: Path):
    # Duplicated from the generation on purpose: a checkpoint directory should
    # be readable in isolation.
    meta = CheckpointMeta(
        checkpoint_id="checkpoint_1",
        generation_id="gen-1",
        runsc_version="runsc 1",
        spec_fingerprint="sha256:spec",
        rootfs_id="sha256:rootfs",
        imports=("pandas", "numpy"),
    )
    payload = json.loads(meta.write(tmp_path).read_text())

    assert payload["checkpoint_id"] == "checkpoint_1"
    assert payload["rootfs_id"] == "sha256:rootfs"
    assert payload["imports"] == ["pandas", "numpy"]
    assert payload["producer"] == "pipeline"


def test_the_plan_is_written_atomically_and_reads_back(tmp_path: Path):
    path = tmp_path / "checkpoints.json"
    write_plan(path, [{"imports": ["pandas"]}])

    assert json.loads(path.read_text()) == [{"imports": ["pandas"]}]
    assert list(tmp_path.glob("*.partial")) == []
