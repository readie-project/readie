"""The distributions a plan needs installed in the rootfs."""

from __future__ import annotations

from pathlib import Path

from readie_pipeline.metadata.models import Metadata, PackageFacts
from readie_pipeline.planning.ports import CheckpointPlan
from readie_pipeline.planning.requirements import (
    distributions_for,
    requirement_lines,
    write_requirements,
)


def facts(name: str, distribution: str = "", loaded: tuple[str, ...] = ()) -> PackageFacts:
    return PackageFacts(
        base_import=name,
        distribution=distribution,
        loaded_modules=frozenset(loaded) | {name},
        disk_size_mb=1.0,
        memory_size_mb=1.0,
        import_time=0.1,
    )


def metadata(*entries: PackageFacts) -> Metadata:
    return Metadata({f.base_import: f for f in entries})


def test_a_plans_imports_map_to_their_distributions():
    meta = metadata(facts("sklearn", "scikit-learn"), facts("pandas", "pandas"))
    plan = CheckpointPlan(imports=("sklearn", "pandas"))

    assert distributions_for([plan], meta) == ("pandas", "scikit-learn")


def test_what_an_import_loads_is_installed_too():
    # pandas imports numpy, so a rootfs with pandas and no numpy cannot import it,
    # even though the plan never names numpy.
    meta = metadata(
        facts("pandas", "pandas", loaded=("numpy",)),
        facts("numpy", "numpy"),
    )

    assert distributions_for([CheckpointPlan(imports=("pandas",))], meta) == ("numpy", "pandas")


def test_a_loaded_module_with_no_entry_uses_the_package_it_sits_under():
    meta = metadata(
        facts("scipy", "scipy", loaded=("numpy.core._multiarray_umath",)),
        facts("numpy", "numpy"),
    )

    assert distributions_for([CheckpointPlan(imports=("scipy",))], meta) == ("numpy", "scipy")


def test_the_standard_library_and_unmeasured_modules_add_nothing():
    meta = metadata(
        facts("pandas", "pandas", loaded=("json", "mystery")),
        facts("json", ""),
    )

    assert distributions_for([CheckpointPlan(imports=("pandas",))], meta) == ("pandas",)


def test_every_checkpoint_counts_once():
    meta = metadata(facts("numpy", "numpy"), facts("scipy", "scipy"))
    plans = [
        CheckpointPlan(imports=("numpy",)),
        CheckpointPlan(imports=("numpy", "scipy")),
    ]

    assert distributions_for(plans, meta) == ("numpy", "scipy")


def test_an_empty_plan_needs_nothing():
    assert distributions_for([], metadata(facts("numpy", "numpy"))) == ()


def test_the_file_is_one_requirement_per_line_with_a_comment_header(tmp_path: Path):
    path = tmp_path / "nested" / "rootfs-requirements.txt"

    write_requirements(path, ["numpy==2.0.2", "pandas"])

    lines = path.read_text().splitlines()
    assert lines[0].startswith("#")
    assert [line for line in lines if not line.startswith("#")] == ["numpy==2.0.2", "pandas"]


def test_each_distribution_is_pinned_to_the_version_the_metadata_recorded():
    meta = Metadata(
        {"sklearn": facts("sklearn", "scikit-learn")},
        distributions={"scikit-learn": "1.6.1", "numpy": "2.0.2"},
    )

    # Looked up by canonical name, and the requirement keeps the name as recorded.
    assert requirement_lines(["scikit-learn", "numpy"], meta) == (
        "scikit-learn==1.6.1",
        "numpy==2.0.2",
    )


def test_a_distribution_with_no_recorded_version_is_listed_by_name():
    meta = Metadata({}, distributions={"numpy": "2.0.2"})

    assert requirement_lines(["numpy", "wurlitzer"], meta) == ("numpy==2.0.2", "wurlitzer")


def test_a_local_version_label_stays_in_the_pin():
    # Pinned: dropping +cpu selected PyPI's CUDA build of torch, which needs an
    # nvidia-nccl-cu12 that conflicts with the one pinned beside it.
    meta = Metadata({}, distributions={"torch": "2.10.0+cpu"})

    assert requirement_lines(["torch"], meta) == ("torch==2.10.0+cpu",)
