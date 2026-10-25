"""Saving an ``analyze`` run as it goes, and deciding whether an earlier one can be used."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from readie_pipeline.metadata.models import Metadata, MetadataError, PackageFacts
from readie_pipeline.metadata.progress import (
    ProgressJournal,
    incompatibility,
    partial_path,
    read_progress,
)

INSTALLED = {"numpy": "2.0.2", "pandas": "2.3.3"}


@pytest.fixture
def journal() -> Iterator[Callable[..., ProgressJournal]]:
    """Opens journals, and closes every one of them when the test is over."""
    opened: list[ProgressJournal] = []

    def make(path: Path, *, resume: bool = False, samples: int = 3) -> ProgressJournal:
        made = ProgressJournal(
            path, samples=samples, base_image="image@sha256:aa", installed=INSTALLED, resume=resume
        )
        opened.append(made)
        return made

    yield make
    for made in opened:
        made.close()


def facts(name: str, *, seconds: float = 1.0, error: str = "") -> PackageFacts:
    return PackageFacts(
        base_import=name,
        distribution=name,
        loaded_modules=frozenset({name, f"{name}.core"}),
        memory_size_mb=5.0,
        import_time=seconds,
        error=error,
    )


def test_the_side_file_sits_beside_the_metadata_file(tmp_path):
    assert partial_path(tmp_path / "metadata" / "cpu.json") == tmp_path / "metadata" / (
        "cpu.json.partial"
    )


def test_what_was_journaled_is_read_back(tmp_path, journal):
    path = tmp_path / "cpu.json.partial"
    j = journal(path)
    j.package("numpy", facts("numpy", seconds=0.4))
    j.resolution("numpy.linalg", "numpy")

    known, samples = read_progress(path)

    assert samples == 3
    assert known.base_image == "image@sha256:aa"
    assert known.distributions == INSTALLED
    assert known.packages["numpy"].import_time == 0.4
    assert known.packages["numpy"].loaded_modules == frozenset({"numpy", "numpy.core"})
    assert known.resolved == {"numpy.linalg": "numpy"}


def test_each_package_is_on_disk_before_the_next_one_is_measured(tmp_path, journal):
    # The point of journaling per package: a stop between two packages loses neither.
    path = tmp_path / "cpu.json.partial"
    j = journal(path)
    j.package("numpy", facts("numpy"))

    assert "numpy" in read_progress(path)[0].packages  # read while the journal is still open


def test_a_package_that_failed_is_not_journaled_so_it_is_tried_again(tmp_path, journal):
    path = tmp_path / "cpu.json.partial"
    j = journal(path)
    j.package("broken", facts("broken", error="could not profile: timed out"))

    assert read_progress(path)[0].packages == {}


def test_a_new_journal_starts_the_file_again(tmp_path, journal):
    path = tmp_path / "cpu.json.partial"
    journal(path).package("numpy", facts("numpy"))

    journal(path, resume=False)

    assert read_progress(path)[0].packages == {}


def test_a_resumed_journal_keeps_what_is_there_and_adds_to_it(tmp_path, journal):
    path = tmp_path / "cpu.json.partial"
    journal(path).package("numpy", facts("numpy"))

    journal(path, resume=True).package("pandas", facts("pandas"))

    assert set(read_progress(path)[0].packages) == {"numpy", "pandas"}


def test_a_resumed_journal_with_no_file_starts_one(tmp_path, journal):
    path = tmp_path / "cpu.json.partial"
    journal(path, resume=True).package("numpy", facts("numpy"))
    assert set(read_progress(path)[0].packages) == {"numpy"}


def test_a_line_cut_short_by_a_crash_is_skipped_and_nothing_before_it_is_lost(tmp_path, journal):
    path = tmp_path / "cpu.json.partial"
    j = journal(path)
    j.package("numpy", facts("numpy"))
    j.package("pandas", facts("pandas"))
    text = path.read_text()
    path.write_text(text[: len(text) - 40])  # the last line is cut in the middle

    known, _ = read_progress(path)

    assert set(known.packages) == {"numpy"}


def test_a_file_without_a_header_is_not_an_unfinished_run(tmp_path):
    path = tmp_path / "cpu.json.partial"
    path.write_text('{"packages": {}}\n')
    with pytest.raises(MetadataError, match="no readable header"):
        read_progress(path)


def test_removing_the_journal_deletes_the_file(tmp_path, journal):
    path = tmp_path / "cpu.json.partial"
    j = journal(path)
    j.remove()
    assert not path.exists()


def known(**kwargs: object) -> Metadata:
    base: dict[str, object] = {"distributions": dict(INSTALLED), "base_image": "image@sha256:aa"}
    return Metadata({}, **{**base, **kwargs})  # type: ignore[arg-type]


def test_metadata_from_the_same_image_and_versions_can_be_reused():
    assert incompatibility(known(), base_image="image@sha256:aa", installed=INSTALLED) == ""


def test_metadata_from_another_image_cannot_be_reused():
    reason = incompatibility(known(), base_image="image@sha256:bb", installed=INSTALLED)
    assert "image@sha256:aa" in reason
    assert "image@sha256:bb" in reason


def test_an_image_that_was_not_named_is_not_held_against_the_metadata():
    assert incompatibility(known(), base_image="", installed=INSTALLED) == ""


def test_a_different_version_of_a_distribution_cannot_be_reused():
    reason = incompatibility(
        known(), base_image="image@sha256:aa", installed={**INSTALLED, "numpy": "2.1.0"}
    )
    assert "numpy 2.0.2 (now 2.1.0)" in reason


def test_a_distribution_that_is_no_longer_installed_cannot_be_reused():
    reason = incompatibility(known(), base_image="image@sha256:aa", installed={"numpy": "2.0.2"})
    assert "pandas 2.3.3 (now not installed)" in reason


def test_metadata_with_no_recorded_versions_cannot_be_checked_and_is_not_reused():
    reason = incompatibility(known(distributions={}), base_image="", installed=INSTALLED)
    assert "no distribution versions" in reason
