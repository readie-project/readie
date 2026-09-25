"""Measure what each package costs to have available.

Three numbers per package: disk size, the resident memory it adds once
imported, and the time to import it with its dependencies already resident.
The last two have to be measured in a subprocess -- an import is cached after
the first one, so timing or memory-sampling it in-process measures a
dictionary lookup, not the real cost. Disk size and resident memory are not
proportional to each other per package (a package can unpack to a small
on-disk footprint but allocate a much larger one in memory, or the reverse),
and it is memory, not disk, that gVisor actually has to copy back on restore --
see ``PackageFacts.memory_size_mb``.

What a package actually loads is discovered empirically, not declared: a raw
dotted import string (e.g. ``sklearn.svm.LinearSVC``, which is not itself an
importable module -- ``LinearSVC`` is a class) is resolved to the deepest
importable prefix (``sklearn.svm``, see ``metadata/resolve.py``), and that
resolved name's own ``sys.modules`` diff -- everything actually loaded to have
it resident, parent chain and external dependencies included -- is its full,
already-transitive closure. There is no top-level collapse: ``sklearn`` and
``sklearn.svm`` are independent, separately-measured nodes, because a bare
``import sklearn`` does not necessarily load everything ``sklearn.svm`` does
(the same way bare ``import PIL`` never loads ``PIL.Image``).

A node reachable more than once -- shared by two other nodes that were both
requested -- is only ever measured once: ``analyze`` memoizes on the resolved
name as it walks.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from importlib.metadata import PackageNotFoundError, distribution, packages_distributions
from pathlib import Path

from readie_pipeline.metadata.models import Metadata, PackageFacts

#: Imports the dependencies first, then times and memory-samples the target
#: alone. Without the warm-up both measurements are dominated by whatever the
#: target pulls in, which is not what a checkpoint of *this* package saves.
_PROFILER = """
import json, sys, time

def _vmrss_mb():
    # /proc/self/status, not resource.getrusage: ru_maxrss is a
    # monotonically increasing peak, not a point-in-time snapshot, so a
    # heavy dependency warmed up just before a small target would pollute
    # that target's own before/after delta.
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0

dependencies, target = json.loads(sys.argv[1]), sys.argv[2]
for name in dependencies:
    try:
        __import__(name)
    except BaseException:
        pass

before_mb = _vmrss_mb()
start = time.perf_counter()
try:
    __import__(target)
    elapsed = time.perf_counter() - start
    memory_mb = max(_vmrss_mb() - before_mb, 0.0)
    print(json.dumps({"time": elapsed, "memory_mb": memory_mb}))
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


def _distribution_for(name: str, provided: Mapping[str, list[str]]) -> str:
    """Best-effort PyPI distribution name for ``name``'s top-level segment.

    Diagnostic only: ``loaded_modules``, ``memory_size_mb`` and ``import_time``
    are all measured empirically and never depend on this being right, unlike
    the previous ``Requires-Dist``-based walk, which needed a correct
    distribution name to find a package's declared dependencies at all.
    """
    distributions = provided.get(name.split(".", 1)[0])
    return distributions[0] if distributions else ""


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


def _discover(candidate: str) -> tuple[str | None, frozenset[str]]:
    """Resolve ``candidate`` and diff ``sys.modules`` around it, in a subprocess.

    Delegates to ``metadata/resolve.py``'s ``discover`` (run via ``-m`` so it
    is one process per distinct candidate, never sharing state with this one
    or with the profiling subprocess below). ``(None, frozenset())`` on
    anything that stops the subprocess from reporting cleanly -- a hang, a
    timeout, or a candidate that resolves to nothing importable.

    Deliberately *not* catching ``OSError`` here: with a fixed argv
    (``sys.executable``, a constant module path -- nothing candidate-specific
    ever reaches the command line), the only realistic way this ``run`` call
    itself raises ``OSError`` is a failed ``fork``/``exec`` -- resource
    exhaustion (out of memory, file descriptors, or processes), which is a
    statement about *this* process's own health, not about ``candidate``. A
    child that crashes or gets OOM-killed doesn't raise ``OSError`` here at
    all -- it just returns a bad exit code and garbled output, which the
    ``JSONDecodeError`` below already handles. Swallowing a real
    resource-exhaustion error and recording it as "candidate doesn't
    resolve" would be silently, systematically wrong for however much of the
    rest of the run hits the same exhaustion afterwards -- a crash here is
    the correct, loud signal that something needs investigating, not a
    result to catch.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "readie_pipeline.metadata.resolve", candidate],
            capture_output=True,
            text=True,
            timeout=_PROFILE_TIMEOUT,
            check=False,
        )
        payload = json.loads(completed.stdout.strip() or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None, frozenset()

    resolved = payload.get("resolved")
    return resolved, frozenset(payload.get("loaded") or ())


def _eligible_for_warmup(loaded: frozenset[str], resolved: str) -> set[str]:
    """Names in ``loaded`` that might be safe to independently measure and warm up.

    A name is unsafe exactly when it is ``resolved`` itself or a dotted
    descendant of it (e.g. ``pandas._libs.foo`` when ``resolved`` is
    ``pandas``): Python always imports a parent before its children, so
    warming up a descendant of ``resolved`` would silently import ``resolved``
    itself first, leaving nothing left to time when ``resolved`` is what gets
    profiled next -- it would read as free. Ancestors (``sklearn`` when
    ``resolved`` is ``sklearn.svm``) and unrelated names (``numpy``) carry no
    such risk on their own.

    This is only a first cut, not the final answer: it says nothing about
    whether one of these candidates is itself wholly owned by *another*
    candidate also in ``loaded`` (see ``_visit``, which prunes those
    dynamically as it measures each candidate's own closure) -- that
    distinction needs each candidate's own measured closure, which is not
    known yet at this point.
    """
    return {m for m in loaded if m != resolved and not m.startswith(resolved + ".")}


def _profile(warm: list[str], target: str) -> dict[str, object]:
    """Run the profiling subprocess, warming ``warm`` untimed before timing ``target``.

    Returns the parsed measurement (``{"time": ..., "memory_mb": ...}``) or
    ``{"error": "..."}``. Isolated in its own function so a test can
    monkeypatch it directly instead of needing a real installed package to
    exercise ``_visit``'s own logic.

    Deliberately *not* catching ``OSError`` here, for the same reason as
    ``_discover`` above: this call's argv is fixed too, so an ``OSError``
    from ``run`` itself can only mean this process's own resources are
    exhausted, not that ``target`` failed to import -- and letting that
    crash loudly beats silently recording a real package as "failed to
    profile" for a reason that has nothing to do with the package.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c", _PROFILER, json.dumps(warm), target],
            capture_output=True,
            text=True,
            timeout=_PROFILE_TIMEOUT,
            check=False,
        )
        return dict(json.loads(completed.stdout.strip() or "{}"))
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"error": f"could not profile: {exc}"}


def _is_excluded(candidate: str, exclude: frozenset[str]) -> bool:
    """Whether ``candidate`` falls under any excluded prefix.

    Checked against the *raw* candidate, before resolution: resolution only
    ever shortens a dotted name from the right (see ``resolve.py``), never
    changes its leading segments, so whatever a candidate would resolve to
    always shares the same prefix it already has. A candidate is excluded if
    it equals an excluded name exactly, or is a dotted descendant of one --
    ``"google"`` excludes ``"google.cloud.aiplatform.base"`` too.
    """
    return any(candidate == name or candidate.startswith(name + ".") for name in exclude)


def _cached_discover(
    candidate: str,
    discovered: dict[str, tuple[str | None, frozenset[str]]],
) -> tuple[str | None, frozenset[str]]:
    """``_discover(candidate)``, memoizing only a successful resolution.

    See ``_visit``'s own docstring for why a failure is deliberately never
    cached here: a subprocess killed mid-import can leave cross-contaminating
    state behind on the shared container filesystem, so a failure observed
    once is not reliably a property of ``candidate`` itself, and every
    independent path that reaches a failing name still deserves its own
    fresh, unbiased attempt.
    """
    if candidate in discovered:
        return discovered[candidate]
    resolved, loaded = _discover(candidate)
    if resolved is not None:
        discovered[candidate] = (resolved, loaded)
    return resolved, loaded


@dataclass
class _Frame:
    """One in-progress node in the iterative walk below.

    The heap-allocated stand-in for a single ``_visit`` call's local
    variables in the old recursive version: ``pos`` is how far through
    ``eligible`` this node has gotten, ``warm``/``covered`` accumulate the
    same way the old loop's locals did, and ``waiting_on`` names the child
    (if any) this frame is currently blocked on -- set right before pushing
    that child's own frame, and consulted (then cleared) the next time this
    frame is back on top, exactly where the old code would have resumed
    after a recursive call returned.
    """

    resolved: str
    loaded: frozenset[str]
    eligible: list[str]
    pos: int = 0
    warm: list[str] = field(default_factory=list)
    covered: set[str] = field(default_factory=set)
    waiting_on: str | None = None


def _start_frame(
    candidate: str,
    *,
    exclude: frozenset[str],
    discovered: dict[str, tuple[str | None, frozenset[str]]],
    results: dict[str, PackageFacts],
    visiting: set[str],
) -> tuple[str | None, _Frame | None]:
    """Resolve ``candidate`` and either settle it immediately or open a frame.

    Returns ``(resolved, None)`` when there is nothing further to do --
    excluded, never resolves, already in ``results``, or already ``visiting``
    (a cycle: the frame further down the stack that is already open for this
    name will finish measuring it once the cycle unwinds) -- or ``(resolved,
    frame)`` with a fresh frame added to ``visiting`` and ready for its own
    eligible names to be walked.
    """
    if _is_excluded(candidate, exclude):
        return None, None
    resolved, loaded = _cached_discover(candidate, discovered)
    if resolved is None:
        return None, None
    if resolved in results:
        return resolved, None
    if resolved in visiting:
        return resolved, None
    visiting.add(resolved)
    eligible = sorted(_eligible_for_warmup(loaded, resolved))
    return resolved, _Frame(resolved=resolved, loaded=loaded, eligible=eligible)


def _finalize(frame: _Frame, *, provided: Mapping[str, list[str]]) -> PackageFacts:
    """Profile a frame whose eligible names have all been walked already."""
    dist = _distribution_for(frame.resolved, provided)
    facts = PackageFacts(
        base_import=frame.resolved,
        distribution=dist,
        loaded_modules=frame.loaded,
        disk_size_mb=_installed_size_mb(dist, frame.resolved) if dist else 0.0,
    )
    measured = _profile(frame.warm, frame.resolved)
    if "error" in measured:
        return replace(facts, error=str(measured["error"]))
    return replace(
        facts,
        import_time=float(measured.get("time", 0.0)),  # type: ignore[arg-type]
        memory_size_mb=float(measured.get("memory_mb", 0.0)),  # type: ignore[arg-type]
    )


def _visit(
    candidate: str,
    *,
    provided: Mapping[str, list[str]],
    results: dict[str, PackageFacts],
    visiting: set[str],
    exclude: frozenset[str],
    discovered: dict[str, tuple[str | None, frozenset[str]]],
    on_progress: object,
) -> str | None:
    """Resolve and measure ``candidate``, walking its closure with an explicit stack.

    Returns the resolved name, or ``None`` if ``candidate`` never resolves to
    anything importable *or* falls under ``exclude`` (see ``_is_excluded``) --
    checked first, before ever spawning a subprocess for it, so an excluded
    subtree costs nothing at all, not even a discovery pass. This is the only
    way to actually stop spending time on a package once analysis has started
    walking its closure: there is no partial-progress checkpoint, so cutting
    a subtree off requires knowing before the fact, not fixing mid-run.

    This used to recurse directly (one Python call per node), guarded against
    *cycles* by ``visiting`` -- but a cycle is not the only way a real
    dependency graph gets deep. TensorFlow's own graph turned out to have a
    single, genuinely non-cyclic chain long enough (984+ distinct names, each
    waiting on the next before it could be measured) to exceed Python's
    default call-stack depth outright, raising an uncaught ``RecursionError``
    that crashed the whole run -- confirmed against a real base image, not a
    hypothetical concern. ``visiting`` correctly prevented infinite recursion;
    it never bounded finite-but-deep recursion, because that bound is
    ``sys.getrecursionlimit()``, not anything about the graph itself. Walking
    with an explicit stack of ``_Frame``s (``_start_frame``/``_finalize``
    above) instead of the Python call stack has no such limit regardless of
    how deep or complex a package's dependency graph turns out to be.

    ``results`` is the memo, keyed by resolved name: a name reached by two
    different candidates (or reached twice) is only ever measured once.

    ``visiting`` guards against a real cycle in the empirical closure (two
    packages whose own internals import each other, which real packages do)
    -- a plain ``results``-membership check is not enough on its own, because
    ``results[resolved]`` is only set once a name's frame is fully processed.
    Without a separate in-progress marker, a cycle back to a node still open
    looks unvisited and opens a second frame for it, unboundedly.

    Folding a name into an already-measured node's own cost (skipping its own
    discover+profile pair entirely) is only ever safe when that name is
    *provably* exclusive to that one node -- which dotted descent is
    (``pandas._libs.foo`` under ``pandas``): Python guarantees a child is
    never importable without its parent already resident, so every possible
    path to it necessarily shares the same ancestor, and folding it there can
    never miss a different, unrelated context that also needed pricing.
    A same-level sibling with no such guarantee (e.g. Keras 3's
    ``keras.src.applications.vgg16``, re-exported by the differently-named
    public shim ``keras.applications.vgg16`` but not provably exclusive to
    it -- some other public shim could share the same internal utility)
    cannot be safely folded into whichever node happens to discover it
    first: a request needing only that other context would then silently
    drop the shared name's real cost entirely, summing to zero instead of
    its true weight. (Tried exactly this as a generalization of the pruning
    below and caught it via ``test_a_shared_dependency_is_only_ever_profiled_once``,
    which starts failing the moment a genuinely shared, non-descendant name
    like ``numpy`` no longer gets its own entry.) Handling that pattern
    safely needs ``exclude`` instead: an excluded name is never independently
    measured *or* artificially warmed up for anyone else, so it simply stays
    part of whatever real, uncached import chain naturally triggers it --
    correct for every toucher, at the cost of that name's real cost being
    paid again inside every one of them, in exchange for skipping its own
    wasted discover+profile pair.

    ``discovered`` (see ``_cached_discover``) memoizes a *successful*
    ``_discover`` call by its raw candidate string -- separate from, and much
    narrower than, ``results``. A name still ``visiting`` when a different,
    unrelated sibling also lists it as eligible is reached again and again
    before its one owning frame finishes (confirmed against a real base
    image: some names were independently rediscovered 30+ times, each a real
    ~5-10s subprocess, while the owning frame was still deep in its own,
    separate walk) -- every one of those repeats discards the freshly
    (re)discovered pair immediately at the ``in visiting`` check without ever
    using it, so caching it removes pure waste. It cannot change *what* gets
    measured: the eligible set and every priced ``results`` entry are
    governed entirely by ``results``/``visiting`` membership, never by
    whether this cache had an answer.
    """
    root_resolved, root_frame = _start_frame(
        candidate, exclude=exclude, discovered=discovered, results=results, visiting=visiting
    )
    if root_frame is None:
        return root_resolved

    stack: list[_Frame] = [root_frame]

    while stack:
        frame = stack[-1]

        if frame.waiting_on is not None:
            # Resuming after a pushed child frame (below, now popped) has
            # fully finished -- exactly where the old recursive call would
            # have returned control here.
            other = frame.waiting_on
            frame.waiting_on = None
            other_facts = results.get(other)
            if other_facts is not None:
                frame.warm.append(other)
                frame.covered |= other_facts.loaded_modules
            frame.pos += 1
            continue

        if frame.pos >= len(frame.eligible):
            facts = _finalize(frame, provided=provided)
            results[frame.resolved] = facts
            visiting.discard(frame.resolved)
            if callable(on_progress):
                on_progress(frame.resolved, facts)
            stack.pop()
            continue

        other = frame.eligible[frame.pos]
        if other in frame.covered:
            frame.pos += 1
            continue
        if other in results:
            other_facts = results[other]
            frame.warm.append(other)
            frame.covered |= other_facts.loaded_modules
            frame.pos += 1
            continue

        _, other_frame = _start_frame(
            other, exclude=exclude, discovered=discovered, results=results, visiting=visiting
        )
        if other_frame is not None:
            frame.waiting_on = other
            stack.append(other_frame)
            continue

        # The resolved name (discarded above) is either None (excluded /
        # unresolvable) or already resolved but not yet in results (still
        # visiting elsewhere, a cycle) -- either way, nothing to warm up from
        # it, matching the old recursive version's behaviour when
        # results.get(other) was None right after its own recursive call
        # returned.
        frame.pos += 1

    return root_resolved


def analyze(
    import_names: Iterable[str],
    *,
    exclude: Iterable[str] = (),
    on_progress: object = None,
) -> Metadata:
    """Measure every named import, resolved, and its full empirical closure.

    A raw name that never resolves to anything importable is recorded with an
    error under its own raw spelling rather than skipped, so a later run can
    tell "not measured" from "measured as free" -- the same guarantee the
    previous distribution-based walk gave for a package with no installed
    distribution.

    ``exclude`` names packages (and everything under them, e.g. ``"google"``
    excludes ``google.cloud.aiplatform`` too) to skip entirely -- not even a
    discovery subprocess is spawned for them, in this candidate's own
    top-level visit or anywhere they turn up during someone else's closure
    walk (see ``_is_excluded``). For a subtree large and slow enough on its
    own to dominate a whole run (confirmed against a real base image: some of
    ``google.cloud.aiplatform``'s own generated submodules alone cost 15-25s
    *each* to even attempt importing), this is the only way to bound the
    work -- there is no partial-progress checkpoint to fall back on once a
    run has started walking into one.
    """
    provided = packages_distributions()
    results: dict[str, PackageFacts] = {}
    resolved_map: dict[str, str] = {}
    visiting: set[str] = set()
    discovered: dict[str, tuple[str | None, frozenset[str]]] = {}
    excluded = frozenset(exclude)

    for raw in sorted({name for name in import_names if name}):
        resolved = _visit(
            raw,
            provided=provided,
            results=results,
            visiting=visiting,
            exclude=excluded,
            discovered=discovered,
            on_progress=on_progress,
        )
        if resolved is not None:
            resolved_map[raw] = resolved
        elif raw not in results:
            error = (
                "excluded from analysis"
                if _is_excluded(raw, excluded)
                else "could not be resolved to an importable module"
            )
            facts = PackageFacts(base_import=raw, error=error)
            results[raw] = facts
            if callable(on_progress):
                on_progress(raw, facts)

    return Metadata(results, resolved=resolved_map)
