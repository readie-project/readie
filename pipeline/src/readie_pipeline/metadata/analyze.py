"""Coordinate analysis without depending on a resource's loading implementation."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace

from readie_pipeline.metadata.inventory import canonical_name, installed_versions
from readie_pipeline.metadata.models import Metadata, ResourceFacts
from readie_pipeline.metadata.progress import Journal
from readie_pipeline.resources.handlers import default_analyzers
from readie_pipeline.resources.identifiers import ResourceType, parse_resource
from readie_pipeline.resources.packages import ColdProfile
from readie_pipeline.resources.ports import AnalysisContext, ResourceAnalyzer


def _matches(identifier: str, prefixes: frozenset[str]) -> bool:
    return any(
        identifier == prefix
        or identifier.startswith(prefix + ".")
        or identifier.startswith(prefix + "/")
        or identifier.startswith(prefix + ":")
        for prefix in prefixes
    )


def analyze(
    identifiers: Iterable[str],
    *,
    cold: ColdProfile,
    exclude: Iterable[str] = (),
    on_progress: object = None,
    known: Metadata | None = None,
    keep_timings: bool = False,
    discovery: Metadata | None = None,
    journal: Journal | None = None,
    priority: Mapping[str, int] | None = None,
    defer: Iterable[str] = (),
    analyzers: Mapping[ResourceType, ResourceAnalyzer] | None = None,
    after_persist: Callable[[str], None] | None = None,
    on_start: Callable[[str], None] | None = None,
) -> Metadata:
    """Dispatch IDs while sharing resume, exclusion, progress and ordering policy."""
    requested = frozenset(identifiers)
    handlers = default_analyzers(requested) if analyzers is None else analyzers
    excluded, deferred = frozenset(exclude), frozenset(defer)
    weights = priority or {}
    result = Metadata(
        dict(known.resources) if known is not None and keep_timings else {},
        distributions=dict(known.distributions) if known is not None else {},
        base_image=known.base_image if known is not None else "",
        resolved=dict(known.resolved) if known is not None else {},
    )
    prior = discovery or known or Metadata()

    def report(identifier: str, facts: ResourceFacts) -> None:
        if _matches(identifier, excluded):
            return
        result.resources[identifier] = facts
        if callable(on_progress):
            on_progress(identifier, facts)
        if journal is not None:
            journal.resource(identifier, facts)
            if after_persist is not None:
                after_persist(identifier)

    visiting: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            return
        visiting.add(identifier)
        kind, name = parse_resource(identifier)
        if _matches(identifier, excluded):
            return
        resolved = result.resolved.get(identifier, identifier)
        if resolved in result.resources and result.resources[resolved].usable:
            return
        handler = handlers.get(kind)
        if handler is None:
            report(
                identifier,
                ResourceFacts(
                    base_import=name, resource_type=kind, error=f"no analyzer registered for {kind}"
                ),
            )
            return
        for required in handler.requires(identifier):
            visit(required)
        if on_start is not None:
            on_start(identifier)
        measured = handler.analyze(
            identifier, AnalysisContext(result, prior, cold, report, excluded)
        )
        for key, facts in measured.resources.items():
            if key not in result.resources or result.resources[key] != facts:
                report(key, replace(facts, dependencies=result.closure(facts.dependencies)))
        result.distributions.update(measured.distributions)
        for raw, resolved in measured.resolved.items():
            result.resolved[raw] = resolved
            if journal is not None:
                journal.resolution(raw, resolved)

    for identifier in sorted(
        requested, key=lambda key: (_matches(key, deferred), -weights.get(key, 0), key)
    ):
        visit(identifier)
    versions = installed_versions()
    return replace(
        result,
        distributions={
            **result.distributions,
            **{
                canonical_name(f.distribution): versions[canonical_name(f.distribution)]
                for f in result.resources.values()
                if canonical_name(f.distribution) in versions
            },
        },
    )
