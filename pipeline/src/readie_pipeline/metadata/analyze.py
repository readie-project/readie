"""Measure what each package costs to have available.

Two numbers per package: disk size, and the time to import it with its
dependencies already resident. The second is what a checkpoint saves, and it has
to be measured in a subprocess -- an import is cached after the first one, so
timing it in-process measures a dictionary lookup.

A package's dependencies are walked and measured too, recursively down to
packages with no dependencies of their own, so a closure over the result (see
``Metadata.closure``) has a real number for every package it reaches rather than
falling back to the planner's nominal default. A package reachable more than
once -- shared by two packages that were both requested -- is only ever
profiled once: ``analyze`` memoizes on import name as it walks.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, distribution, packages_distributions
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

from readie_pipeline.metadata.models import Metadata, PackageFacts

#: Imports the dependencies first, then times the target alone. Without the
#: warm-up the measurement is dominated by whatever the target pulls in, which
#: is not what a checkpoint of *this* package saves.
_PROFILER = """
import json, sys, time

dependencies, target = json.loads(sys.argv[1]), sys.argv[2]
for name in dependencies:
    try:
        __import__(name)
    except BaseException:
        pass

start = time.perf_counter()
try:
    __import__(target)
    print(json.dumps({"time": time.perf_counter() - start}))
except BaseException as exc:
    print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
"""

#: A package that hangs on import must not hang the analysis.
_PROFILE_TIMEOUT = 120.0


#: This tool's own top-level package name. Analysing a real base image runs
#: this stage's interpreter with the copied rootfs's dist-packages also on its
#: PYTHONPATH (see pipeline/Dockerfile), so a plain sys.path scan reports
#: readie-pipeline itself as if it were part of that base image -- it never
#: is, so it is excluded below regardless of what else is on the path.
#: Everything *else* this tool depends on (numpy, packaging) genuinely is also
#: shipped by a real base image independently, and PYTHONPATH's ordering
#: (prepended, so it is searched first) is what makes importing and measuring
#: those resolve to the base image's own copy rather than this tool's --
#: confirmed against a real build, not a name to special-case here.
_SELF = __name__.partition(".")[0]

#: Also excluded, unlike _SELF, this one genuinely is baked into every base
#: image -- the rootfs stage in pipeline/Dockerfile installs it there so a
#: sandbox can run `python -m readie_executor` -- but no request's own code
#: ever imports the harness that is running it, so it is never actionable for
#: the planner and would just be permanent, unused clutter in the metadata.
_HARNESS_ONLY = frozenset({"readie_executor"})


def installed_packages() -> list[str]:
    """Every top-level import name installed in the current environment.

    "The current environment" is the base image: this is meant to run wherever
    a checkpoint's rootfs would import from, so the result is what that
    checkpoint actually has available -- not just whatever a corpus of sample
    requests happens to reference. A package the corpus never mentions but the
    base image ships is still measured, so the planner can score it too.
    """
    excluded = _HARNESS_ONLY | {_SELF}
    return sorted(name for name in packages_distributions() if name not in excluded)


def top_level_imports(distribution_name: str) -> list[str]:
    r"""Import names a distribution provides.

    ``top_level.txt`` is newline-separated. The previous implementation split on
    ``'\n'`` written inside a non-raw string -- so it split on the literal two
    characters backslash and n, and a distribution providing several modules
    yielded one nonsense name.
    """
    try:
        dist = distribution(distribution_name)
    except PackageNotFoundError:
        return [distribution_name.replace("-", "_")]

    try:
        listed = dist.read_text("top_level.txt")
    except (OSError, UnicodeDecodeError):
        listed = None

    if listed:
        names = [line.strip() for line in listed.splitlines() if line.strip()]
        if names:
            return names

    return [distribution_name.replace("-", "_")]


def _dependencies(dist_requires: Iterable[str] | None) -> dict[str, str]:
    """Applicable requirements, with environment markers evaluated."""
    resolved: dict[str, str] = {}
    for raw in dist_requires or []:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            # A malformed requirement in someone else's metadata is not this
            # tool's problem, and is not worth failing an analysis over.
            continue
        if requirement.marker and not requirement.marker.evaluate():
            continue
        resolved[requirement.name] = str(requirement.specifier) or "(any)"
    return resolved


def _installed_size_mb(dist_name: str, import_name: str) -> float:
    """Total size of the files a distribution installs under an import name."""
    try:
        dist = distribution(dist_name)
    except PackageNotFoundError:
        return 0.0

    prefix = import_name.replace(".", "/")
    total = 0
    for entry in dist.files or []:
        text = str(entry)
        if text != import_name and not text.startswith(prefix):
            continue
        try:
            path = Path(str(dist.locate_file(entry)))
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total / (1024 * 1024)


def analyze_package(import_name: str, dist_name: str) -> PackageFacts:
    """Measure one package."""
    try:
        dist = distribution(dist_name)
    except PackageNotFoundError:
        return PackageFacts(base_import=import_name, error="distribution not installed")

    dependencies = _dependencies(dist.requires)

    warm: list[str] = []
    for name in dependencies:
        warm.extend(top_level_imports(name))

    facts = PackageFacts(
        base_import=import_name.split(".", maxsplit=1)[0],
        distribution=dist_name,
        dependencies=dependencies,
        disk_size_mb=_installed_size_mb(dist_name, import_name),
    )

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c", _PROFILER, json.dumps(warm), import_name],
            capture_output=True,
            text=True,
            timeout=_PROFILE_TIMEOUT,
            check=False,
        )
        measured = json.loads(completed.stdout.strip() or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        return replace(facts, error=f"could not profile: {exc}")

    if "error" in measured:
        return replace(facts, error=str(measured["error"]))

    return replace(facts, import_time=float(measured.get("time", 0.0)))


def _visit(
    name: str,
    *,
    provided: Mapping[str, list[str]],
    results: dict[str, PackageFacts],
    on_progress: object,
) -> None:
    """Measure ``name`` and recurse into its dependencies, unless already done.

    ``results`` is the memo: a name is added to it before its dependencies are
    visited, so a cycle (rare, but requirements permit one) terminates instead
    of recursing forever, and a dependency shared by two packages is measured
    on the first visit only.
    """
    if name in results:
        return

    distributions = provided.get(name)
    if not distributions:
        facts = PackageFacts(
            base_import=name, error="no installed distribution provides this import"
        )
        results[name] = facts
        if callable(on_progress):
            on_progress(name, facts)
        return

    facts = analyze_package(name, distributions[0])
    results[name] = facts
    if callable(on_progress):
        on_progress(name, facts)

    for dependency_distribution in facts.dependencies:
        for dependency_name in top_level_imports(dependency_distribution):
            # top_level_imports falls back to guessing an import name from the
            # distribution name when there is no top_level.txt to read (common
            # for modern wheels) -- Flask, beautifulsoup4 and PyWavelets all
            # lack one here, and the guess ("Flask", not "flask") matches
            # nothing installed. That is a wrong guess, not a package that
            # failed to analyse, so it is skipped silently rather than
            # recorded as an unmeasured one -- unlike an explicitly requested
            # name (see analyze() below), which still gets an error entry.
            if dependency_name in provided or dependency_name in results:
                _visit(dependency_name, provided=provided, results=results, on_progress=on_progress)


def analyze(import_names: Iterable[str], *, on_progress: object = None) -> Metadata:
    """Measure every named package that is installed here, and its full dependency closure.

    Packages with no installed distribution are recorded with an error rather
    than skipped, so a later run can tell "not measured" from "measured as
    free". The previous implementation guarded with ``if x not in d: pass`` --
    which does nothing -- and then indexed ``d.get(x)[0]``, raising TypeError on
    exactly the case the guard was written for.
    """
    provided = packages_distributions()
    results: dict[str, PackageFacts] = {}

    for import_name in sorted(set(import_names)):
        _visit(
            import_name.split(".")[0], provided=provided, results=results, on_progress=on_progress
        )

    return Metadata(results)
