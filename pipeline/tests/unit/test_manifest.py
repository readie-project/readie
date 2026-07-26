"""The manifests the worker reads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from crfs_pipeline.config import EXECUTOR_PROTOCOL
from crfs_pipeline.manifest import CheckpointMeta, Manifest, write_plan


def manifest(**overrides: Any) -> Manifest:
    base: dict[str, Any] = {
        "runsc_version": "runsc version release-20260721.0",
        "spec_fingerprint": "sha256:spec",
        "python_path": "/lib/python3.12/dist-packages",
        "overlay": "root:memory",
        "network": "none",
    }
    return Manifest(**{**base, **overrides})


def test_the_manifest_records_argv_not_a_path():
    # The executor is an installed wheel, so there is no stable source file to
    # point at. The worker replays this verbatim rather than assembling its own.
    payload = manifest().to_json()
    assert payload["executor_argv"] == ["python", "-u", "-m", "crfs_executor"]
    assert "executor_entrypoint" not in payload


def test_the_manifest_records_the_protocol_version():
    # The worker refuses artifacts whose executor speaks a format it does not
    # implement. Without this field it would default to 1 and be refused.
    assert manifest().to_json()["executor_protocol"] == EXECUTOR_PROTOCOL


def test_the_manifest_carries_no_identity_of_its_own():
    # The worker image tag names the build. An id here would be a second name
    # for the same thing, free to disagree with the tag.
    payload = manifest().to_json()
    assert "id" not in payload
    assert "generation_id" not in payload


def test_the_manifest_carries_no_rootfs_id():
    # There is exactly one rootfs, baked into the image beside these
    # checkpoints, so there is nothing to identify -- and the pairing is correct
    # by construction rather than by a field nobody ever compared.
    assert "rootfs_id" not in manifest().to_json()


def test_every_field_the_go_reader_requires_is_present():
    # worker/internal/artifact.Manifest.Validate insists on these.
    payload = manifest().to_json()
    for name in ("runsc_version", "spec_fingerprint", "executor_argv"):
        assert payload[name], name


def test_the_timestamp_is_rfc3339_utc():
    # time.Time in Go parses this; a local-time stamp would fail to unmarshal.
    stamp = manifest().to_json()["created_at"]
    assert stamp.endswith("Z")
    assert len(stamp) == len("2026-01-01T00:00:00Z")


def test_writing_is_atomic(tmp_path: Path):
    # A manifest half-written by an interrupted build is worse than none: the
    # worker would load it and refuse everything for a reason that has nothing
    # to do with the checkpoints.
    manifest().write(tmp_path)

    assert (tmp_path / "manifest.json").is_file()
    assert list(tmp_path.glob("*.partial")) == []


def test_the_manifest_is_named_what_the_worker_looks_for(tmp_path: Path):
    # artifact.ManifestFileName on the Go side.
    path = manifest().write(tmp_path)
    assert path.name == "manifest.json"
    assert json.loads(path.read_text())["runsc_version"].startswith("runsc")


def test_checkpoint_meta_is_self_describing(tmp_path: Path):
    # Duplicated from the manifest on purpose: a checkpoint directory should be
    # readable in isolation.
    meta = CheckpointMeta(
        checkpoint_id="checkpoint_1",
        runsc_version="runsc 1",
        spec_fingerprint="sha256:spec",
        imports=("pandas", "numpy"),
    )
    payload = json.loads(meta.write(tmp_path).read_text())

    assert payload["checkpoint_id"] == "checkpoint_1"
    assert payload["imports"] == ["pandas", "numpy"]
    assert payload["producer"] == "pipeline"


def test_checkpoint_meta_carries_no_generation_or_rootfs_id(tmp_path: Path):
    payload = json.loads(
        CheckpointMeta(checkpoint_id="checkpoint_1", runsc_version="r", spec_fingerprint="s")
        .write(tmp_path)
        .read_text()
    )

    assert "generation_id" not in payload
    assert "rootfs_id" not in payload


def test_the_plan_is_written_atomically_and_reads_back(tmp_path: Path):
    path = tmp_path / "checkpoints.json"
    write_plan(path, [{"imports": ["pandas"]}])

    assert json.loads(path.read_text()) == [{"imports": ["pandas"]}]
    assert list(tmp_path.glob("*.partial")) == []
