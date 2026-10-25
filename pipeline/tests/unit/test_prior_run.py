"""Which earlier run ``analyze`` builds on: one to continue, one to reuse, or none."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from readie_pipeline import cli
from readie_pipeline.config import Settings
from readie_pipeline.metadata.models import Metadata, PackageFacts
from readie_pipeline.metadata.progress import ProgressJournal, partial_path

INSTALLED = {"numpy": "2.0.2"}
IMAGE = "image@sha256:aa"


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    monkeypatch.setattr(cli, "installed_versions", lambda: INSTALLED)


def settings_in(tmp_path: Path, *, samples: int = 3) -> Settings:
    return Settings(
        bundle_dir=tmp_path / "bundle",
        output_dir=tmp_path / "out",
        data_dir=tmp_path / "data",
        base_image=IMAGE,
        profile_samples=samples,
    )


def facts() -> PackageFacts:
    return PackageFacts(base_import="numpy", loaded_modules=frozenset({"numpy"}), import_time=0.4)


def write_finished(settings: Settings, *, versions: dict[str, str] = INSTALLED) -> None:
    settings.metadata_path.parent.mkdir(parents=True)
    metadata = Metadata(
        {"numpy": facts()}, distributions=dict(versions), base_image=IMAGE, resolved={}
    )
    settings.metadata_path.write_text(json.dumps(metadata.to_json()))


def write_unfinished(settings: Settings, *, samples: int = 3, image: str = IMAGE) -> None:
    journal = ProgressJournal(
        partial_path(settings.metadata_path),
        samples=samples,
        base_image=image,
        installed=INSTALLED,
        resume=False,
    )
    journal.package("numpy", facts())
    journal.close()


def test_with_nothing_left_behind_there_is_nothing_to_build_on(tmp_path):
    assert cli._prior_run(settings_in(tmp_path)) == (None, False)


def test_an_unfinished_run_of_the_same_kind_is_continued(tmp_path, capsys):
    settings = settings_in(tmp_path)
    settings.metadata_path.parent.mkdir(parents=True)
    write_unfinished(settings)

    known, keep_timings = cli._prior_run(settings)

    assert keep_timings is True
    assert known is not None
    assert set(known.packages) == {"numpy"}
    assert "continuing" in capsys.readouterr().out


def test_an_unfinished_run_with_another_number_of_timings_is_not_continued(tmp_path, capsys):
    settings = settings_in(tmp_path, samples=3)
    settings.metadata_path.parent.mkdir(parents=True)
    write_unfinished(settings, samples=1)

    assert cli._prior_run(settings) == (None, False)
    assert "took 1 timing(s) per package" in capsys.readouterr().out


def test_an_unfinished_run_from_another_image_is_not_continued(tmp_path, capsys):
    settings = settings_in(tmp_path)
    settings.metadata_path.parent.mkdir(parents=True)
    write_unfinished(settings, image="image@sha256:bb")

    assert cli._prior_run(settings) == (None, False)
    assert "image@sha256:bb" in capsys.readouterr().out


def test_a_finished_file_gives_its_discovery_but_not_its_timings(tmp_path, capsys):
    settings = settings_in(tmp_path)
    write_finished(settings)

    known, keep_timings = cli._prior_run(settings)

    assert keep_timings is False
    assert known is not None
    assert set(known.packages) == {"numpy"}
    assert "timings are measured again" in capsys.readouterr().out


def test_a_finished_file_from_other_versions_is_not_used(tmp_path):
    settings = settings_in(tmp_path)
    write_finished(settings, versions={"numpy": "1.26.0"})

    assert cli._prior_run(settings) == (None, False)


def test_an_unusable_unfinished_run_falls_back_to_the_finished_file(tmp_path):
    settings = settings_in(tmp_path)
    write_finished(settings)
    partial_path(settings.metadata_path).write_text("not a journal\n")

    known, keep_timings = cli._prior_run(settings)

    assert keep_timings is False
    assert known is not None
