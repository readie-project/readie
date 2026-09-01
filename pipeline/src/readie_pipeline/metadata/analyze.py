"""Measure what each package costs to have available.

Two numbers per package: disk size, and the time to import it with its
dependencies already resident. The second is what a checkpoint saves, and it has
to be measured in a subprocess -- an import is cached after the first one, so
timing it in-process measures a dictionary lookup.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterable
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


def analyze(import_names: Iterable[str], *, on_progress: object = None) -> Metadata:
    """Measure every named package that is installed here.

    Packages with no installed distribution are recorded with an error rather
    than skipped, so a later run can tell "not measured" from "measured as
    free". The previous implementation guarded with ``if x not in d: pass`` --
    which does nothing -- and then indexed ``d.get(x)[0]``, raising TypeError on
    exactly the case the guard was written for.
    """
    provided = packages_distributions()
    results: dict[str, PackageFacts] = {}

    for import_name in sorted(set(import_names)):
        top_level = import_name.split(".")[0]
        distributions = provided.get(top_level)

        if not distributions:
            results[import_name] = PackageFacts(
                base_import=top_level, error="no installed distribution provides this import"
            )
            continue

        results[import_name] = analyze_package(import_name, distributions[0])
        if callable(on_progress):
            on_progress(import_name, results[import_name])

    return Metadata(results)
