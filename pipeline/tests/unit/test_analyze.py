"""What gets measured, as opposed to how it is measured.

The real resolver and profiler subprocesses need actual installed packages and
are exercised by hand against a real environment, not in this suite: tests
below fake ``_discover`` (resolution + the empirical ``sys.modules`` closure)
and ``_profile`` (the timing/memory subprocess), the two seams that would
otherwise need one.
"""

from __future__ import annotations

from readie_pipeline.metadata import analyze


def test_installed_packages_is_every_top_level_name_this_environment_provides(monkeypatch):
    # Not the corpus's imports: a checkpoint restores into the base image, so
    # that -- whatever is actually importable here -- is the universe to measure.
    monkeypatch.setattr(
        analyze,
        "packages_distributions",
        lambda: {"pandas": ["pandas"], "sklearn": ["scikit-learn"]},
    )
    assert analyze.installed_packages() == ["pandas", "sklearn"]


def test_installed_packages_is_sorted_so_a_run_diffs_cleanly(monkeypatch):
    monkeypatch.setattr(
        analyze, "packages_distributions", lambda: {"zlib": ["zlib"], "abc": ["abc"]}
    )
    assert analyze.installed_packages() == ["abc", "zlib"]


def test_installed_packages_is_empty_in_a_bare_environment(monkeypatch):
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    assert analyze.installed_packages() == []


def test_installed_packages_excludes_this_tool_itself(monkeypatch):
    # Analysing a real base image runs this tool's own interpreter with the
    # copied rootfs also on its PYTHONPATH (see pipeline/Dockerfile), so a
    # plain sys.path scan reports readie-pipeline itself as if it were part of
    # that base image -- confirmed against a real build, where it showed up
    # alongside pandas and torch as an "installed package."  It never is one.
    monkeypatch.setattr(
        analyze,
        "packages_distributions",
        lambda: {"pandas": ["pandas"], "readie_pipeline": ["readie-pipeline"]},
    )
    assert analyze.installed_packages() == ["pandas"]


def test_installed_packages_excludes_the_executor_harness(monkeypatch):
    # readie_executor genuinely is baked into every base image (the rootfs
    # stage in pipeline/Dockerfile installs it so a sandbox can run
    # `python -m readie_executor`) -- confirmed against a real build -- but no
    # request's own code ever imports the harness running it, so it is never
    # actionable for the planner and would just be permanent, unused clutter.
    monkeypatch.setattr(
        analyze,
        "packages_distributions",
        lambda: {"pandas": ["pandas"], "readie_executor": ["readie-executor"]},
    )
    assert analyze.installed_packages() == ["pandas"]


# ---------------------------------------------------------------------------
# Eligibility for warm-up (the marginal-measurement rule)
# ---------------------------------------------------------------------------
def test_ancestors_and_unrelated_names_are_eligible_for_warmup():
    # sklearn (an ancestor of sklearn.svm) and numpy (unrelated) can both be
    # independently imported without requiring sklearn.svm itself -- safe to
    # measure on their own and warm up.
    loaded = frozenset({"sklearn.svm", "sklearn", "numpy"})
    assert analyze._eligible_for_warmup(loaded, "sklearn.svm") == {"sklearn", "numpy"}


def test_a_node_itself_is_never_eligible_for_its_own_warmup():
    loaded = frozenset({"pandas", "numpy"})
    assert "pandas" not in analyze._eligible_for_warmup(loaded, "pandas")


def test_descendants_are_never_eligible_for_warmup():
    # Warming up pandas._libs.foo would silently import pandas first (Python
    # always imports a parent before its children), leaving nothing left to
    # time when pandas itself is what gets profiled -- it would read as free.
    loaded = frozenset({"pandas", "pandas._libs.foo", "numpy"})
    eligible = analyze._eligible_for_warmup(loaded, "pandas")
    assert "pandas._libs.foo" not in eligible
    assert eligible == {"numpy"}


# ---------------------------------------------------------------------------
# _visit / analyze
# ---------------------------------------------------------------------------
def _fake_graph(monkeypatch, graph: dict[str, tuple[str | None, frozenset[str]]]):
    """``graph[candidate] = (resolved, loaded)``, standing in for ``_discover``."""
    monkeypatch.setattr(
        analyze, "_discover", lambda candidate: graph.get(candidate, (None, frozenset()))
    )


def test_a_candidate_that_never_resolves_gets_an_error_entry_under_its_own_name(monkeypatch):
    _fake_graph(monkeypatch, {})
    monkeypatch.setattr(analyze, "packages_distributions", dict)

    metadata = analyze.analyze(["nonexistent"])

    assert metadata.packages["nonexistent"].error == "could not be resolved to an importable module"
    assert "nonexistent" not in metadata.resolved


def test_on_progress_fires_even_when_nothing_resolves(monkeypatch):
    _fake_graph(monkeypatch, {})
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    seen: list[tuple[str, str]] = []

    analyze.analyze(
        ["nonexistent"], on_progress=lambda name, facts: seen.append((name, facts.error))
    )

    assert seen == [("nonexistent", "could not be resolved to an importable module")]


def test_a_resolved_candidate_is_recorded_in_the_resolved_map(monkeypatch):
    _fake_graph(monkeypatch, {"pandas": ("pandas", frozenset({"pandas"}))})
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(
        analyze, "_profile", lambda _warm, _target: {"time": 0.3, "memory_mb": 40.0}
    )

    metadata = analyze.analyze(["pandas"])

    assert metadata.resolved == {"pandas": "pandas"}
    assert metadata.packages["pandas"].import_time == 0.3
    assert metadata.packages["pandas"].memory_size_mb == 40.0


def test_a_deeper_candidate_resolves_to_its_shorter_prefix(monkeypatch):
    # What corpus/tree_parser.py now hands this: the deepest candidate implied
    # by `from sklearn.svm import LinearSVC`, which is not itself a module.
    _fake_graph(
        monkeypatch,
        {"sklearn.svm.LinearSVC": ("sklearn.svm", frozenset({"sklearn", "sklearn.svm"}))},
    )
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(analyze, "_profile", lambda _warm, _target: {"time": 0.1, "memory_mb": 5.0})

    metadata = analyze.analyze(["sklearn.svm.LinearSVC"])

    assert metadata.resolved == {"sklearn.svm.LinearSVC": "sklearn.svm"}
    assert "sklearn.svm" in metadata.packages
    assert "sklearn.svm.LinearSVC" not in metadata.packages


def test_a_shared_dependency_is_only_ever_profiled_once(monkeypatch):
    # pandas and scipy both (empirically) load numpy; numpy must get exactly
    # one facts entry and be warmed up for each of the others, not re-profiled.
    _fake_graph(
        monkeypatch,
        {
            "pandas": ("pandas", frozenset({"pandas", "numpy"})),
            "scipy": ("scipy", frozenset({"scipy", "numpy"})),
            "numpy": ("numpy", frozenset({"numpy"})),
        },
    )
    monkeypatch.setattr(analyze, "packages_distributions", dict)

    calls: list[tuple[tuple[str, ...], str]] = []

    def fake_profile(warm: list[str], target: str) -> dict[str, object]:
        calls.append((tuple(sorted(warm)), target))
        return {"time": 0.1, "memory_mb": 1.0}

    monkeypatch.setattr(analyze, "_profile", fake_profile)

    metadata = analyze.analyze(["pandas", "scipy"])

    assert set(metadata.packages) == {"pandas", "scipy", "numpy"}
    numpy_calls = [c for c in calls if c[1] == "numpy"]
    assert len(numpy_calls) == 1, "numpy must be profiled exactly once despite two paths to it"
    # Both pandas and scipy warm numpy up before their own timed import.
    assert ("numpy",) in [c[0] for c in calls if c[1] in ("pandas", "scipy")]


def test_an_internal_submodule_owned_by_an_ancestor_is_never_independently_measured(
    monkeypatch,
):
    # scipy.linalg's own internals (e.g. _solve_toeplitz) are always loaded as
    # a side effect of importing scipy.linalg itself -- once scipy.linalg is
    # independently measured (it must be: bare `import scipy` here does NOT
    # load linalg, so scipy.linalg is not "covered" by scipy alone), its own
    # closure already accounts for _solve_toeplitz. sklearn's own closure also
    # contains _solve_toeplitz (Python registers every ancestor of anything it
    # imports), but that must not earn it a second, wasted *profile* pass just
    # because it also happens to turn up there -- confirmed against a real
    # base image, where exactly this caused dozens of redundant passes per
    # large package. It must never get an independent *measurement*, i.e.
    # its own entry in ``metadata.packages``, no matter which node reaches
    # it first.
    graph = {
        "sklearn": (
            "sklearn",
            frozenset({"sklearn", "scipy", "scipy.linalg", "scipy.linalg._solve_toeplitz"}),
        ),
        "scipy": ("scipy", frozenset({"scipy"})),
        "scipy.linalg": (
            "scipy.linalg",
            frozenset({"scipy", "scipy.linalg", "scipy.linalg._solve_toeplitz"}),
        ),
    }
    discovered: list[str] = []

    def fake_discover(candidate: str) -> tuple[str | None, frozenset[str]]:
        discovered.append(candidate)
        return graph.get(candidate, (None, frozenset()))

    monkeypatch.setattr(analyze, "_discover", fake_discover)
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(analyze, "_profile", lambda _warm, _target: {"time": 0.1, "memory_mb": 1.0})

    metadata = analyze.analyze(["sklearn"])

    assert set(metadata.packages) == {"sklearn", "scipy", "scipy.linalg"}


def test_a_sibling_not_related_by_descent_still_gets_its_own_measurement(monkeypatch):
    # Keras 3's actual layout: its public API (keras.applications.vgg16) is a
    # thin shim that re-exports from a *parallel*, differently-named internal
    # namespace (keras.src.applications.vgg16) -- siblings by name, never
    # related by dotted descent, even though importing one always loads the
    # other. Unlike a true descendant (pandas._libs.foo under pandas, which
    # Python guarantees can never be reached any other way), a same-level
    # sibling is not *provably* exclusive to the one node that happened to
    # discover it first -- some other public shim could reach the same
    # internal name too -- so folding it away by default would risk silently
    # dropping a genuinely shared dependency's cost for any other context
    # that needs it (see test_a_shared_dependency_is_only_ever_profiled_once,
    # which is exactly this risk with numpy). It must still get its own
    # entry unless the operator explicitly excludes that subtree (see
    # test_an_excluded_name_discovered_via_someone_elses_closure_is_also_skipped).
    graph = {
        "keras.applications.vgg16": (
            "keras.applications.vgg16",
            frozenset({"keras", "keras.applications.vgg16", "keras.src.applications.vgg16"}),
        ),
        "keras.src.applications.vgg16": (
            "keras.src.applications.vgg16",
            frozenset({"keras", "keras.src.applications.vgg16"}),
        ),
    }
    discovered: list[str] = []

    def fake_discover(candidate: str) -> tuple[str | None, frozenset[str]]:
        discovered.append(candidate)
        return graph.get(candidate, (None, frozenset()))

    monkeypatch.setattr(analyze, "_discover", fake_discover)
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(analyze, "_profile", lambda _warm, _target: {"time": 0.1, "memory_mb": 1.0})

    metadata = analyze.analyze(["keras.applications.vgg16"])

    assert "keras.src.applications.vgg16" in discovered
    assert set(metadata.packages) == {"keras.applications.vgg16", "keras.src.applications.vgg16"}


def _counting_discover(monkeypatch, graph: dict[str, tuple[str | None, frozenset[str]]]):
    """Like ``_fake_graph``, but returns a per-candidate call counter too."""
    calls: dict[str, int] = {}

    def fake_discover(candidate: str) -> tuple[str | None, frozenset[str]]:
        calls[candidate] = calls.get(candidate, 0) + 1
        return graph.get(candidate, (None, frozenset()))

    monkeypatch.setattr(analyze, "_discover", fake_discover)
    return calls


def test_a_name_still_visiting_is_only_ever_discovered_once(monkeypatch):
    # "owner" is reached three times before it finishes measuring itself: its
    # own initial call, and once more from each of the two siblings it pulls
    # in (both of which, realistically, also list "owner" back in their own
    # closure -- Python registers every ancestor everywhere it turns up).
    # Every one of those later reaches finds "owner" still `visiting` and
    # bails out immediately without using the freshly (re)discovered
    # (resolved, loaded) pair at all -- so the second and third discover
    # calls are pure waste, caught by caching the first one.
    calls = _counting_discover(
        monkeypatch,
        {
            "owner": ("owner", frozenset({"owner", "sib1", "sib2"})),
            "sib1": ("sib1", frozenset({"sib1", "owner", "sib2"})),
            "sib2": ("sib2", frozenset({"sib2", "owner", "sib1"})),
        },
    )
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(analyze, "_profile", lambda _warm, _target: {"time": 0.1, "memory_mb": 1.0})

    metadata = analyze.analyze(["owner"])

    assert calls["owner"] == 1, "owner is reached 3x but must only be discovered once"
    assert calls["sib1"] == 1, "sib1 is reached 2x (from owner, and from sib2) but only once"
    assert set(metadata.packages) == {"owner", "sib1", "sib2"}


def test_a_failed_resolution_is_never_cached_and_is_retried_every_time(monkeypatch):
    # "bad" fails to resolve (not in the graph) and is eligible from two
    # separate, unrelated siblings -- unlike a successful discovery, this
    # must NOT be cached: a failure observed once is not reliably a property
    # of the candidate (see _visit's own docstring on cross-subprocess
    # filesystem contamination), so every independent path gets its own
    # unbiased attempt.
    calls = _counting_discover(
        monkeypatch,
        {
            "root": ("root", frozenset({"root", "path1", "path2"})),
            "path1": ("path1", frozenset({"path1", "bad"})),
            "path2": ("path2", frozenset({"path2", "bad"})),
        },
    )
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(analyze, "_profile", lambda _warm, _target: {"time": 0.1, "memory_mb": 1.0})

    analyze.analyze(["root"])

    assert calls["bad"] == 2, "a failing candidate must be retried by each independent path"


def test_a_genuinely_deep_non_cyclic_chain_does_not_overflow_the_stack(monkeypatch):
    # A cycle is not the only way a real dependency graph gets deep: a single,
    # non-repeating chain -- each node eligible for the next, never for
    # itself or an earlier one -- can also run thousands of names long
    # (TensorFlow's own graph did, against a real base image: 984+ distinct
    # frames deep, an uncaught RecursionError, and the whole run crashing).
    # `visiting` only ever guarded against a name reappearing while still
    # open; it never bounded plain depth, because that bound was Python's
    # call stack, not the graph. A chain deeper than the default recursion
    # limit (1000) proves the walk no longer depends on that stack at all.
    depth = 5000
    graph: dict[str, tuple[str | None, frozenset[str]]] = {
        f"n{i}": (f"n{i}", frozenset({f"n{i}", f"n{i + 1}"})) for i in range(depth - 1)
    }
    graph[f"n{depth - 1}"] = (f"n{depth - 1}", frozenset({f"n{depth - 1}"}))
    _fake_graph(monkeypatch, graph)
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(analyze, "_profile", lambda _warm, _target: {"time": 0.1, "memory_mb": 1.0})

    metadata = analyze.analyze(["n0"])

    assert set(metadata.packages) == {f"n{i}" for i in range(depth)}
    assert all(metadata.packages[f"n{i}"].usable for i in range(depth))


def test_a_genuine_cycle_terminates_instead_of_recursing_forever(monkeypatch):
    # Real packages can empirically appear to depend on each other (each
    # other's own sys.modules diff includes the other) -- confirmed against a
    # real base image, where this produced a RecursionError with the exact
    # same two stack frames repeating hundreds of times, not a legitimately
    # deep dependency chain. `results[resolved]` is only set at the *end* of
    # `_visit`, after recursing, so a plain results-membership check alone
    # cannot catch a cycle back to a node still mid-recursion.
    _fake_graph(
        monkeypatch,
        {
            "a": ("a", frozenset({"a", "b"})),
            "b": ("b", frozenset({"b", "a"})),
        },
    )
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(analyze, "_profile", lambda _warm, _target: {"time": 0.1, "memory_mb": 1.0})

    metadata = analyze.analyze(["a"])

    assert set(metadata.packages) == {"a", "b"}
    assert metadata.packages["a"].usable
    assert metadata.packages["b"].usable


def test_a_profiling_failure_still_records_the_resolution(monkeypatch):
    # Resolution and profiling are separate subprocess passes -- a package
    # that resolves fine but hangs or errors during profiling must still be
    # recorded as resolved, so Metadata.resolve_imports still includes it
    # (with a nominal fallback cost) rather than dropping it as if it had
    # never been requested at all.
    _fake_graph(monkeypatch, {"torch": ("torch", frozenset({"torch"}))})
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(
        analyze, "_profile", lambda _warm, _target: {"error": "TimeoutExpired: ..."}
    )

    metadata = analyze.analyze(["torch"])

    assert metadata.resolved == {"torch": "torch"}
    assert metadata.packages["torch"].error
    assert not metadata.packages["torch"].usable


def test_disk_size_is_measured_from_the_resolved_names_own_distribution(monkeypatch):
    _fake_graph(monkeypatch, {"pandas": ("pandas", frozenset({"pandas"}))})
    monkeypatch.setattr(analyze, "packages_distributions", lambda: {"pandas": ["pandas"]})
    monkeypatch.setattr(analyze, "_installed_size_mb", lambda _dist, _name: 12.5)
    monkeypatch.setattr(analyze, "_profile", lambda _warm, _target: {"time": 0.1, "memory_mb": 1.0})

    metadata = analyze.analyze(["pandas"])

    assert metadata.packages["pandas"].disk_size_mb == 12.5
    assert metadata.packages["pandas"].distribution == "pandas"


# ---------------------------------------------------------------------------
# Excluding a subtree
# ---------------------------------------------------------------------------
def test_is_excluded_matches_the_name_itself_and_its_descendants():
    exclude = frozenset({"google"})
    assert analyze._is_excluded("google", exclude)
    assert analyze._is_excluded("google.cloud.aiplatform.base", exclude)
    assert not analyze._is_excluded("googlemaps", exclude)
    assert not analyze._is_excluded("sklearn", exclude)


def test_an_excluded_top_level_candidate_is_never_discovered(monkeypatch):
    discovered: list[str] = []

    def fake_discover(candidate: str) -> tuple[str | None, frozenset[str]]:
        discovered.append(candidate)
        return None, frozenset()

    monkeypatch.setattr(analyze, "_discover", fake_discover)
    monkeypatch.setattr(analyze, "packages_distributions", dict)

    metadata = analyze.analyze(["google.cloud.aiplatform"], exclude=["google"])

    assert discovered == [], "an excluded candidate must not even spawn a discovery subprocess"
    assert metadata.packages["google.cloud.aiplatform"].error == "excluded from analysis"


def test_an_excluded_name_discovered_via_someone_elses_closure_is_also_skipped(monkeypatch):
    # google.cloud.aiplatform never has to be independently requested to be
    # worth excluding -- it turns up in plenty of other packages' own closures
    # too (Python registers every ancestor alongside a deep import), and each
    # of those must not spend a discovery pass on it either.
    discovered: list[str] = []

    def fake_discover(candidate: str) -> tuple[str | None, frozenset[str]]:
        discovered.append(candidate)
        graph = {
            "some-package": (
                "some-package",
                frozenset({"some-package", "numpy", "google.cloud.aiplatform.base"}),
            ),
            "numpy": ("numpy", frozenset({"numpy"})),
        }
        return graph.get(candidate, (None, frozenset()))

    monkeypatch.setattr(analyze, "_discover", fake_discover)
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(analyze, "_profile", lambda _warm, _target: {"time": 0.1, "memory_mb": 1.0})

    metadata = analyze.analyze(["some-package"], exclude=["google"])

    assert "google.cloud.aiplatform.base" not in discovered
    assert set(metadata.packages) == {"some-package", "numpy"}


def test_a_descendant_discovered_out_of_order_is_never_independently_measured(monkeypatch):
    # "w" is alphabetically first, so it is visited before "x" -- and w's own
    # closure happens to also transitively reach "x.y._z", a true descendant
    # of "x" (Python always registers every ancestor of anything it imports).
    # Descent-only pruning relative to whichever node is currently being
    # measured cannot catch this: "x.y._z" is not a descendant of "w" itself,
    # so it looks eligible for its own independent measurement -- even though
    # it is unconditionally a descendant of "x", which this same run will
    # also visit on its own regardless. Confirmed against real data: this
    # produced a genuine double-count risk (sklearn.svm._libsvm_sparse got
    # its own real entry, discovered before sklearn.svm was ever visited,
    # and sklearn.svm's own later measurement then also, unavoidably,
    # included the same cost again). This is exactly why the private-ancestor
    # check must be structural (``_is_owned_by_a_private_ancestor``, checked
    # against every eligible name regardless of who reaches it) rather than
    # relative to whichever node is currently being measured.
    graph = {
        "w": ("w", frozenset({"w", "x.y._z"})),
        "x": ("x", frozenset({"x", "x.y", "x.y._z"})),
        "x.y._z": ("x.y._z", frozenset({"x", "x.y", "x.y._z"})),
    }

    def fake_discover(candidate: str) -> tuple[str | None, frozenset[str]]:
        return graph.get(candidate, (None, frozenset()))

    monkeypatch.setattr(analyze, "_discover", fake_discover)
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(analyze, "_profile", lambda _warm, _target: {"time": 0.1, "memory_mb": 1.0})

    metadata = analyze.analyze(["w", "x"])

    assert set(metadata.packages) == {"w", "x"}


def test_an_explicitly_requested_descendant_still_gets_its_own_measurement(monkeypatch):
    # The flip side of the test above: a name that is itself explicitly
    # requested (a raw top-level candidate, not just reached as someone
    # else's closure member) always gets its own real measurement, even if
    # it also happens to have an ancestor this run also visits.
    graph = {
        "w": ("w", frozenset({"w", "x.y._z"})),
        "x": ("x", frozenset({"x", "x.y", "x.y._z"})),
        "x.y._z": ("x.y._z", frozenset({"x", "x.y", "x.y._z"})),
    }
    discovered: list[str] = []

    def fake_discover(candidate: str) -> tuple[str | None, frozenset[str]]:
        discovered.append(candidate)
        return graph.get(candidate, (None, frozenset()))

    monkeypatch.setattr(analyze, "_discover", fake_discover)
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(analyze, "_profile", lambda _warm, _target: {"time": 0.1, "memory_mb": 1.0})

    metadata = analyze.analyze(["w", "x", "x.y._z"])

    assert "x.y._z" in discovered
    assert set(metadata.packages) == {"w", "x", "x.y._z"}


def test_a_meaningful_non_underscore_name_is_never_folded_just_for_sharing_a_top_level(
    monkeypatch,
):
    # A real regression, caught against real data: "sklearn.svm" is not a
    # private implementation detail of "sklearn" -- it is one of the
    # independent, separately-priced nodes the whole design exists to keep
    # apart ("no top-level collapse": bare `import sklearn` does not
    # necessarily load everything `sklearn.svm` does). But "sklearn" (bare)
    # is *always* in `all_requested` (every top-level installed package is),
    # so a version of the ancestor check that did not also require the
    # leading-underscore condition treated every multi-segment name as
    # foldable purely because its top-level segment is always requested too
    # -- collapsing a real run from 6109 measured entries down to 1273.
    # "sklearn.svm" itself is only ever *reached* here (never a raw request),
    # exactly the shape that triggered the bug: it must still get its own
    # entry despite that.
    graph = {
        "w": ("w", frozenset({"w", "sklearn.svm"})),
        "sklearn": ("sklearn", frozenset({"sklearn"})),
        "sklearn.svm": ("sklearn.svm", frozenset({"sklearn", "sklearn.svm"})),
    }
    discovered: list[str] = []

    def fake_discover(candidate: str) -> tuple[str | None, frozenset[str]]:
        discovered.append(candidate)
        return graph.get(candidate, (None, frozenset()))

    monkeypatch.setattr(analyze, "_discover", fake_discover)
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(analyze, "_profile", lambda _warm, _target: {"time": 0.1, "memory_mb": 1.0})

    metadata = analyze.analyze(["w", "sklearn"])

    assert "sklearn.svm" in discovered
    assert set(metadata.packages) == {"w", "sklearn", "sklearn.svm"}


def test_a_private_name_rooted_in_the_standard_library_is_still_folded(monkeypatch):
    # A real regression, caught against real data: stdlib packages (e.g.
    # "email") are never in installed_packages() at all (it is built from
    # importlib.metadata.packages_distributions(), a third-party-package API
    # that does not enumerate the standard library), so a version of this
    # check that required an ancestor to appear in all_requested silently
    # never fired for anything stdlib-rooted -- "email._header_value_parser"
    # got its own wrongly-separate entry even after the sklearn.svm fix,
    # because "email" was never a member of all_requested to check against.
    # The dotted-path-naming check does not depend on all_requested at all,
    # so it is exactly as true here as for a third-party package.
    graph = {
        "w": ("w", frozenset({"w", "email", "email._header_value_parser"})),
        "email": ("email", frozenset({"email"})),
        "email._header_value_parser": (
            "email._header_value_parser",
            frozenset({"email", "email._header_value_parser"}),
        ),
    }

    def fake_discover(candidate: str) -> tuple[str | None, frozenset[str]]:
        return graph.get(candidate, (None, frozenset()))

    monkeypatch.setattr(analyze, "_discover", fake_discover)
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(analyze, "_profile", lambda _warm, _target: {"time": 0.1, "memory_mb": 1.0})

    metadata = analyze.analyze(["w"])

    assert set(metadata.packages) == {"w", "email"}


def test_a_private_intermediate_segment_folds_its_own_non_underscore_leaf_too(
    monkeypatch,
):
    # The deeper gap: "scipy._lib.array_api_compat" is not itself
    # underscore-prefixed, but it lives inside "scipy._lib", an internal
    # namespace -- so it is just as unconditionally private as a direct
    # underscore-prefixed child would be. Checking only the last segment
    # would miss this; checking every segment after the first catches it.
    graph = {
        "w": ("w", frozenset({"w", "scipy", "scipy._lib", "scipy._lib.array_api_compat"})),
        "scipy": ("scipy", frozenset({"scipy"})),
        "scipy._lib": ("scipy._lib", frozenset({"scipy", "scipy._lib"})),
        "scipy._lib.array_api_compat": (
            "scipy._lib.array_api_compat",
            frozenset({"scipy", "scipy._lib", "scipy._lib.array_api_compat"}),
        ),
    }

    def fake_discover(candidate: str) -> tuple[str | None, frozenset[str]]:
        return graph.get(candidate, (None, frozenset()))

    monkeypatch.setattr(analyze, "_discover", fake_discover)
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(analyze, "_profile", lambda _warm, _target: {"time": 0.1, "memory_mb": 1.0})

    metadata = analyze.analyze(["w", "scipy"])

    assert set(metadata.packages) == {"w", "scipy"}


def test_a_private_name_shared_by_two_unrelated_referrers_is_still_folded(monkeypatch):
    # "shared._internal" is private (last segment starts with "_") and
    # reached by two completely unrelated packages, "w1" and "w2" -- neither
    # is an ancestor or descendant of the other, and neither is an ancestor
    # or descendant of "shared._internal" itself. It is still never
    # independently measured: both w1 and w2 also, unavoidably, reach
    # "shared" (its own nearest non-private ancestor, guaranteed resident by
    # Python's parent-before-child import rule), which is never itself
    # private -- so "shared" alone is always independently measured exactly
    # once and warms both w1 and w2, and "shared._internal"'s cost is
    # already folded into it. Referrer count is irrelevant to safety here.
    graph = {
        "w1": ("w1", frozenset({"w1", "shared", "shared._internal"})),
        "w2": ("w2", frozenset({"w2", "shared", "shared._internal"})),
        "shared": ("shared", frozenset({"shared"})),
        "shared._internal": ("shared._internal", frozenset({"shared", "shared._internal"})),
    }

    def fake_discover(candidate: str) -> tuple[str | None, frozenset[str]]:
        return graph.get(candidate, (None, frozenset()))

    profile_calls: dict[str, list[str]] = {}

    def fake_profile(warm: list[str], target: str) -> dict[str, object]:
        profile_calls[target] = sorted(warm)
        return {"time": 0.1, "memory_mb": 1.0}

    monkeypatch.setattr(analyze, "_discover", fake_discover)
    monkeypatch.setattr(analyze, "packages_distributions", dict)
    monkeypatch.setattr(analyze, "_profile", fake_profile)

    metadata = analyze.analyze(["w1", "w2"])

    assert set(metadata.packages) == {"w1", "w2", "shared"}
    assert "shared" in profile_calls["w1"], "w1 must warm shared away, not capture its cost"
    assert "shared" in profile_calls["w2"], "w2 must warm shared away, not capture its cost"
