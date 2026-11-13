"""Analysis policy applies to resource IDs independently of their handlers."""

from __future__ import annotations

import pytest

from readie_pipeline.metadata.analyze import analyze
from readie_pipeline.metadata.models import Metadata, ResourceFacts
from readie_pipeline.metadata.progress import ProgressJournal, read_progress
from readie_pipeline.resources.identifiers import ResourceType, parse_resource
from readie_pipeline.resources.packages import ColdProfile


class NoDrop:
    def drop(self) -> None:
        pass


class Handler:
    def __init__(self, calls: list[str], requires: tuple[str, ...] = ()) -> None:
        self.calls = calls
        self.dependencies = requires

    def requires(self, identifier):
        return self.dependencies

    def analyze(self, identifier, context):
        self.calls.append(identifier)
        kind, name = parse_resource(identifier)
        return Metadata(
            {
                identifier: ResourceFacts(
                    base_import=name,
                    resource_type=kind,
                    memory_size_mb=1.23456789,
                    disk_size_mb=2.3456789,
                    import_time=0.123456789,
                    dependencies=frozenset(self.dependencies),
                )
            },
            resolved={identifier: identifier},
        )


def run(ids, **kwargs):
    return analyze(ids, cold=ColdProfile(NoDrop(), 1), **kwargs)


@pytest.mark.parametrize("kind", list(ResourceType))
def test_each_resource_type_is_dispatched_to_its_registered_handler(kind):
    calls: list[str] = []
    identifier = f"{kind}:owner/name"
    facts = run([identifier], analyzers={kind: Handler(calls)})
    assert calls == [identifier]
    assert facts.resources[identifier].resource_type == kind


@pytest.mark.parametrize("kind", list(ResourceType))
def test_excluded_ids_do_not_invoke_any_handler(kind):
    calls: list[str] = []
    result = run([f"{kind}:owner/name"], exclude=[str(kind)], analyzers={kind: Handler(calls)})
    assert calls == []
    assert result.resources == {}


@pytest.mark.parametrize("kind", list(ResourceType))
def test_resume_preserves_exact_measurements_without_invoking_the_handler(kind):
    identifier = f"{kind}:owner/name"
    facts = ResourceFacts(
        base_import="owner/name",
        resource_type=kind,
        memory_size_mb=1.23456789,
        disk_size_mb=4.56789123,
        import_time=0.987654321,
    )
    calls: list[str] = []
    result = run(
        [identifier],
        known=Metadata({identifier: facts}),
        keep_timings=True,
        analyzers={kind: Handler(calls)},
    )
    assert calls == []
    assert result.resources[identifier] == facts
    assert result.to_json()["resources"][identifier]["memory_size_mb"] == 1.23456789


def test_priority_and_defer_order_resources_without_source_specific_branches():
    calls: list[str] = []
    handler = Handler(calls)
    run(
        ["package:a", "kaggle_dataset:b", "hf_model:c"],
        analyzers=dict.fromkeys(ResourceType, handler),
        priority={"hf_model:c": 10, "package:a": 2},
        defer=["hf_model"],
    )
    assert calls == ["package:a", "kaggle_dataset:b", "hf_model:c"]


def test_dependencies_are_dispatched_first_and_share_the_resume_state():
    calls: list[str] = []
    dataset = Handler(calls, ("package:pandas",))
    result = run(
        ["kaggle_dataset:owner/data"],
        analyzers={
            ResourceType.PACKAGE: Handler(calls),
            ResourceType.KAGGLE_DATASET: dataset,
        },
    )
    assert calls == ["package:pandas", "kaggle_dataset:owner/data"]
    assert result.closure(["kaggle_dataset:owner/data"]) == {
        "package:pandas",
        "kaggle_dataset:owner/data",
    }


def test_start_events_identify_the_active_resource_without_restarting_known_ids():
    calls: list[str] = []
    started: list[str] = []
    known = Metadata({"package:pandas": ResourceFacts(base_import="pandas")})
    run(
        ["package:pandas", "kaggle_dataset:owner/data"],
        known=known,
        keep_timings=True,
        analyzers={ResourceType.KAGGLE_DATASET: Handler(calls, ("package:pandas",))},
        on_start=started.append,
    )
    assert started == calls == ["kaggle_dataset:owner/data"]


def test_progress_journals_all_resource_types_and_resume_does_not_repeat_them(tmp_path):
    journal = ProgressJournal(
        tmp_path / "progress", samples=1, base_image="test", installed={"numpy": "2"}, resume=False
    )
    calls: list[str] = []
    handlers = {kind: Handler(calls) for kind in ResourceType}
    ids = ["package:numpy", "kaggle_dataset:owner/data"]
    run(ids, analyzers=handlers, journal=journal)
    journal.close()
    known, samples = read_progress(tmp_path / "progress")
    assert samples == 1
    assert set(known.resources) == set(ids)
    calls.clear()
    run(ids, known=known, keep_timings=True, analyzers=handlers)
    assert calls == []


def test_an_unregistered_source_is_reported_without_using_the_package_handler():
    result = run(["hf_model:owner/model"], analyzers={})
    assert "no analyzer registered" in result.resources["hf_model:owner/model"].error


def test_default_package_handler_shares_dependency_measurements_between_roots(monkeypatch):
    from readie_pipeline.resources import packages

    monkeypatch.setattr(packages, "provided_distributions", lambda: {})
    monkeypatch.setattr(packages, "installed_versions", lambda: {})
    observed = {"pandas": frozenset({"pandas", "numpy"}), "numpy": frozenset({"numpy"})}
    monkeypatch.setattr(packages, "_discover", lambda name: (name, observed[name]))
    profiled: list[str] = []

    def profile(warm, target):
        profiled.append(target)
        return {"time": 0.123456789, "memory_mb": 1.23456789}

    monkeypatch.setattr(packages, "_profile", profile)
    result = run(["package:pandas", "package:numpy"])
    assert profiled == ["numpy", "pandas"]
    assert set(result.resources) == {"package:numpy", "package:pandas"}
    assert result.resources["package:pandas"].dependencies == {"package:numpy"}
    assert result.resources["package:pandas"].memory_size_mb == 1.23456789


def test_incremental_package_analysis_never_profiles_finished_dependencies(monkeypatch):
    from readie_pipeline.resources import packages

    monkeypatch.setattr(packages, "provided_distributions", lambda: {})
    monkeypatch.setattr(packages, "installed_versions", lambda: {})
    monkeypatch.setattr(packages, "_discover", lambda name: (name, frozenset({name, "numpy"})))
    profiled: list[str] = []

    def profile(warm, target):
        profiled.append(target)
        return {"time": 0.5, "memory_mb": 2}

    monkeypatch.setattr(packages, "_profile", profile)
    original = ResourceFacts(base_import="numpy", memory_size_mb=7.123456789, import_time=0.123456)
    result = run(["package:pandas"], known=Metadata({"package:numpy": original}), keep_timings=True)
    assert profiled == ["pandas"]
    assert result.resources["package:numpy"] == original


def test_registered_dataset_handler_uses_isolated_samples_and_preserves_package_values(monkeypatch):
    import json
    import subprocess

    original = ResourceFacts(base_import="pandas", memory_size_mb=9.123456789, import_time=0.25)
    known = Metadata(
        {"package:pandas": original, "package:kagglehub": ResourceFacts(base_import="kagglehub")}
    )
    calls = []
    times = iter([3, 1, 2])
    timeouts = []

    def measure(command, **kwargs):
        calls.append(command)
        timeouts.append(kwargs["timeout"])
        seconds = next(times)
        facts = ResourceFacts(
            base_import="owner/data",
            resource_type=ResourceType.KAGGLE_DATASET,
            import_time=seconds,
            memory_size_mb=seconds * 10,
            dependencies=frozenset({"package:pandas", "package:kagglehub"}),
        )
        return subprocess.CompletedProcess(command, 0, json.dumps(facts.to_json()), "")

    monkeypatch.setattr("readie_pipeline.resources.handlers.subprocess.run", measure)
    result = analyze(
        ["kaggle_dataset:owner/data"], cold=ColdProfile(NoDrop(), 3), known=known, keep_timings=True
    )
    assert len(calls) == 3
    assert timeouts == [None, None, None]
    assert all(
        command[-3:] == ["readie_pipeline.resources.kaggle_dataset", "owner/data", "--cold"]
        for command in calls
    )
    dataset = result.resources["kaggle_dataset:owner/data"]
    assert dataset.import_time == 2
    assert dataset.memory_size_mb == 20
    assert result.resources["package:pandas"] == original
