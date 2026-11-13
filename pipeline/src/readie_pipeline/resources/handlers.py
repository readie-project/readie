"""Source-specific adapters for the common analysis coordinator."""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import replace

from readie_pipeline.metadata.models import Metadata, ResourceFacts
from readie_pipeline.resources import packages
from readie_pipeline.resources.identifiers import ResourceType, resource
from readie_pipeline.resources.ports import AnalysisContext, ResourceAnalyzer


def _package_view(metadata: Metadata) -> Metadata:
    """Give the package discovery engine Python module names at its boundary."""
    prefix = "package:"
    return Metadata(
        {
            key[len(prefix) :]: replace(
                facts,
                dependencies=frozenset(
                    dep[len(prefix) :] for dep in facts.dependencies if dep.startswith(prefix)
                ),
            )
            for key, facts in metadata.resources.items()
            if key.startswith(prefix)
        },
        distributions=metadata.distributions,
        base_image=metadata.base_image,
        resolved={
            key[len(prefix) :]: value[len(prefix) :]
            for key, value in metadata.resolved.items()
            if key.startswith(prefix) and value.startswith(prefix)
        },
    )


def _package_facts(facts: ResourceFacts) -> ResourceFacts:
    return replace(
        facts,
        dependencies=frozenset(
            resource(ResourceType.PACKAGE, name)
            for name in facts.dependencies
            if name != facts.base_import
        ),
    )


class PackageAnalyzer:
    """Preserve subprocess discovery, private-module folding and incremental profiling."""

    def __init__(self, requested: Iterable[str]) -> None:
        self._session: packages.PackageSession | None = None
        self._requested = frozenset(
            name.partition(":")[2] for name in requested if name.startswith("package:")
        )

    def requires(self, _identifier: str) -> tuple[str, ...]:
        """Package dependencies are discovered by the package engine."""
        return ()

    def analyze(self, identifier: str, context: AnalysisContext) -> Metadata:
        """Measure one import candidate using the existing package engine."""

        def report(name: str, facts: ResourceFacts) -> None:
            context.report(resource(ResourceType.PACKAGE, name), _package_facts(facts))

        if self._session is None:
            self._session = packages.PackageSession(
                self._requested, _package_view(context.measured), _package_view(context.discovery)
            )
        measured: dict[str, ResourceFacts] = {}

        def collected(name: str, facts: ResourceFacts) -> None:
            key = resource(ResourceType.PACKAGE, name)
            measured[key] = _package_facts(facts)
            report(name, facts)

        resolved = self._session.measure(
            identifier.partition(":")[2],
            cold=context.cold,
            exclude=frozenset(
                name.partition(":")[2] for name in context.excluded if name.startswith("package:")
            ),
            on_progress=collected,
        )
        if resolved is None:
            measured[identifier] = ResourceFacts(
                base_import=identifier.partition(":")[2],
                error="could not be resolved to an importable module",
            )
        return Metadata(
            measured,
            resolved={}
            if resolved is None
            else {identifier: resource(ResourceType.PACKAGE, resolved)},
        )


class KaggleDatasetAnalyzer:
    """Measure datasets in isolated processes using the same cold-sample policy."""

    def requires(self, _identifier: str) -> tuple[str, ...]:
        """The loader's imports are prerequisites for a dataset measurement."""
        return ("package:pandas", "package:kagglehub")

    def analyze(self, identifier: str, context: AnalysisContext) -> Metadata:
        """Download once through KaggleHub's cache and profile each independent load."""
        samples: list[ResourceFacts] = []
        for _ in range(context.cold.samples):
            completed = subprocess.run(  # noqa: S603 - fixed interpreter and module, no shell
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "readie_pipeline.resources.kaggle_dataset",
                    identifier.partition(":")[2],
                    "--cold",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=None,
            )
            try:
                payload = json.loads(completed.stdout)
                facts = ResourceFacts.from_json(identifier, payload)
            except (ValueError, KeyError, TypeError) as exc:
                facts = ResourceFacts(
                    base_import=identifier.partition(":")[2],
                    resource_type=ResourceType.KAGGLE_DATASET,
                    error=f"dataset measurement failed: {completed.stderr.strip() or exc}",
                )
            if facts.error:
                detail = completed.stderr.strip()
                if detail:
                    facts = replace(facts, error=f"{facts.error}: {detail[-4000:]}")
                return Metadata({identifier: facts})
            samples.append(facts)
        measured = replace(
            samples[-1],
            import_time=statistics.median(f.import_time for f in samples),
            memory_size_mb=statistics.median(f.memory_size_mb for f in samples),
        )
        return Metadata({identifier: measured}, resolved={identifier: identifier})


def default_analyzers(requested: Iterable[str]) -> dict[ResourceType, ResourceAnalyzer]:
    """Register source handlers at the composition boundary."""
    return {
        ResourceType.PACKAGE: PackageAnalyzer(requested),
        ResourceType.KAGGLE_DATASET: KaggleDatasetAnalyzer(),
    }
