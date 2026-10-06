"""Corpus and metadata loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from readie_pipeline.corpus.models import Corpus, CorpusError, Request
from readie_pipeline.metadata.models import Metadata, MetadataError, PackageFacts, ResourceType


def request(**kwargs: object) -> Request:
    base: dict[str, object] = {"task_name": "t", "category": "c", "code": "x = 1"}
    return Request(**{**base, **kwargs})  # type: ignore[arg-type]


def _metadata_for(*names: str) -> Metadata:
    """Metadata where each of ``names`` has usable facts keyed by itself.

    ``Metadata.resolve_imports`` falls back to a raw string's own name when
    ``resolved`` has nothing for it and that name already has usable facts --
    this is what lets metadata built by hand, without ever running the real
    resolver, still resolve its own plain package names.
    """
    return Metadata({name: PackageFacts(base_import=name) for name in names})


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------
def test_a_request_reads_the_code_key_the_data_actually_uses():
    # The previous TypedDict declared `code_snippet`, which no entry has ever
    # had. Nothing noticed, because a TypedDict is a dict at runtime.
    entry = {"task_name": "t", "category": "c", "code": "import json"}
    assert Request.from_json(entry, index=0).code == "import json"


def test_a_missing_field_names_the_entry_and_the_field():
    with pytest.raises(CorpusError, match="entry 7 is missing category, code"):
        Request.from_json({"task_name": "t"}, index=7)


def test_a_string_where_a_list_belongs_is_rejected():
    with pytest.raises(CorpusError, match="list of strings"):
        Request.from_json(
            {"task_name": "t", "category": "c", "code": "", "imports": "numpy"}, index=0
        )


def test_import_counts_count_requests_not_import_statements():
    # Two import strings that resolve to the same package are one request
    # that needs it, not two -- `pandas` and `pandas.io` both resolving to
    # `pandas` here, exactly as analyze() would record it.
    metadata = Metadata(
        {"pandas": PackageFacts(base_import="pandas"), "numpy": PackageFacts(base_import="numpy")},
        resolved={"pandas": "pandas", "pandas.io": "pandas", "numpy": "numpy"},
    )
    corpus = Corpus.of([request(imports=("pandas", "pandas.io")), request(imports=("numpy",))])
    assert corpus.import_counts(metadata) == {"numpy": 1, "pandas": 1}


def test_import_counts_are_ordered_most_used_first_then_by_name():
    corpus = Corpus.of(
        [
            request(imports=("b",)),
            request(imports=("b",)),
            request(imports=("a",)),
            request(imports=("c",)),
        ]
    )
    assert list(corpus.import_counts(_metadata_for("a", "b", "c"))) == ["b", "a", "c"]


def test_resources_collects_every_distinct_reference():
    corpus = Corpus.of(
        [
            request(imports=("pandas",), datasets=("d1",), models=("m1",)),
            request(imports=("numpy",), tokenizers=("t1",)),
        ]
    )
    resources = corpus.resources(_metadata_for("pandas", "numpy"))

    assert resources.packages == {"pandas", "numpy"}
    assert resources.datasets == {"d1"}
    assert resources.models == {"m1"}
    assert resources.tokenizers == {"t1"}


def test_an_empty_corpus_has_no_resources():
    assert Corpus.of([]).resources(Metadata({})).packages == frozenset()


def test_a_missing_corpus_says_how_to_make_one(tmp_path: Path):
    with pytest.raises(CorpusError, match="readie-pipeline corpus"):
        Corpus.load(tmp_path / "nope.json")


def test_invalid_json_is_named_as_such(tmp_path: Path):
    path = tmp_path / "corpus.json"
    path.write_text("{not json")
    with pytest.raises(CorpusError, match="not valid JSON"):
        Corpus.load(path)


def test_a_corpus_round_trips_through_disk(tmp_path: Path):
    original = Corpus.of([request(imports=("pandas",), datasets=("d",))])
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps([r.to_json() for r in original]))

    assert Corpus.load(path).requests == original.requests


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
def test_import_time_is_read_from_the_key_the_data_actually_uses():
    # Three names existed for two fields: the writer emitted `load_time`, the
    # committed data has `import_time`, and the TypedDict declared `size`.
    facts = PackageFacts.from_json("pandas", {"import_time": 0.25, "disk_size_mb": 41.4})
    assert facts.import_time == 0.25
    assert facts.disk_size_mb == 41.4


def test_the_older_load_time_spelling_is_still_accepted():
    # Measuring a package costs a subprocess; discarding data over a rename
    # would be worse than accepting both.
    assert PackageFacts.from_json("x", {"load_time": 1.5}).import_time == 1.5


def test_the_resource_type_field_is_actually_emitted():
    # The previous writer used the bare name `type` as a dict key -- the
    # builtin.
    payload = PackageFacts(base_import="pandas").to_json()
    assert payload["resource_type"] == "package"
    assert ResourceType.PACKAGE in set(ResourceType)


def test_an_unmeasured_package_reads_as_free_rather_than_raising():
    # A missing measurement should not stop a plan; the budget still bounds it.
    metadata = Metadata({})
    assert metadata.size_mb("unknown") == 0.0
    assert metadata.import_time("unknown") == 0.0


def test_an_errored_entry_is_not_usable():
    assert not PackageFacts(base_import="x", error="not installed").usable
    assert PackageFacts(base_import="x").usable


def test_an_errored_entry_is_dropped_from_the_written_metadata():
    # Nothing downstream distinguishes "never seen" from "measured and
    # broken" (both cost 0.0), so writing the error down is pure clutter --
    # and, for a raw import string that never resolved, actively misleading
    # clutter, since it then looks like a real package that failed.
    metadata = Metadata(
        {
            "pandas": PackageFacts(base_import="pandas", import_time=0.3),
            "broken": PackageFacts(base_import="broken", error="not installed"),
        }
    )
    payload = metadata.to_json()
    assert set(payload["packages"]) == {"pandas"}
    assert "error" not in payload["packages"]["pandas"]


# ---------------------------------------------------------------------------
# Dependency closures
# ---------------------------------------------------------------------------
# `loaded_modules` is populated from an empirical `sys.modules` diff around
# actually importing a package (see metadata/analyze.py), not from parsing
# declared `Requires-Dist` metadata -- so it is already every module that
# package's own import loads, dependencies included, in one shot. A whole
# category of bug the old distribution-name-based walk had -- resolving a
# dependency's distribution name (e.g. "huggingface-hub") back to the import
# name it was measured under, and getting it wrong across a hyphen/underscore
# spelling mismatch -- cannot happen here at all: there is no distribution
# name in the loop any more, only real, already-canonical module names as
# Python itself reports them.
def test_direct_dependencies_is_everything_a_package_loads_besides_itself():
    metadata = Metadata(
        {
            "pandas": PackageFacts(
                base_import="pandas", loaded_modules=frozenset({"pandas", "numpy"})
            ),
            "numpy": PackageFacts(base_import="numpy", loaded_modules=frozenset({"numpy"})),
        }
    )
    assert metadata.direct_dependencies("pandas") == {"numpy"}


def test_direct_dependencies_of_an_unmeasured_package_is_empty():
    assert Metadata({}).direct_dependencies("unknown") == frozenset()


def test_closure_includes_the_starting_names_and_everything_they_load():
    metadata = Metadata(
        {
            "pandas": PackageFacts(
                base_import="pandas", loaded_modules=frozenset({"pandas", "numpy"})
            ),
            "numpy": PackageFacts(base_import="numpy", loaded_modules=frozenset({"numpy"})),
        }
    )
    assert metadata.closure(["pandas"]) == {"pandas", "numpy"}


def test_closure_is_a_flat_union_not_a_graph_walk():
    # `a`'s own loaded_modules is already fully transitive -- a single
    # sys.modules diff around importing `a` captures `b` and `c` in one shot,
    # exactly as a real analysis would record it -- so closure() only ever
    # needs to union each starting name's own set, never walk further.
    metadata = Metadata(
        {
            "a": PackageFacts(base_import="a", loaded_modules=frozenset({"a", "b", "c"})),
            "b": PackageFacts(base_import="b", loaded_modules=frozenset({"b", "c"})),
            "c": PackageFacts(base_import="c", loaded_modules=frozenset({"c"})),
        }
    )
    assert metadata.closure(["a"]) == {"a", "b", "c"}


def test_closure_visits_a_shared_dependency_once_despite_two_paths_to_it():
    metadata = Metadata(
        {
            "pandas": PackageFacts(
                base_import="pandas", loaded_modules=frozenset({"pandas", "numpy"})
            ),
            "scipy": PackageFacts(
                base_import="scipy", loaded_modules=frozenset({"scipy", "numpy"})
            ),
            "numpy": PackageFacts(base_import="numpy", loaded_modules=frozenset({"numpy"})),
        }
    )
    assert metadata.closure(["pandas", "scipy"]) == {"pandas", "scipy", "numpy"}


def test_closure_of_a_package_with_no_metadata_is_just_itself():
    assert Metadata({}).closure(["unknown"]) == {"unknown"}


def test_metadata_round_trips_and_sorts_for_a_clean_diff(tmp_path: Path):
    metadata = Metadata(
        {
            "zlib": PackageFacts(base_import="zlib", import_time=0.1),
            "abc": PackageFacts(base_import="abc", import_time=0.2),
        }
    )
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(metadata.to_json()))

    assert list(json.loads(path.read_text())["packages"]) == ["abc", "zlib"]
    assert Metadata.load(path).import_time("abc") == 0.2


def test_metadata_still_loads_the_old_flat_format_predating_resolved(tmp_path: Path):
    # Before `resolved` existed, the whole file was just {name: facts} with no
    # wrapper -- a metadata.json written by an older run, or hand-written in a
    # test, must still load.
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({"pandas": {"base_import": "pandas", "import_time": 0.3}}))

    metadata = Metadata.load(path)
    assert metadata.import_time("pandas") == 0.3
    assert metadata.resolved == {}


def test_a_missing_metadata_file_says_how_to_make_one(tmp_path: Path):
    with pytest.raises(MetadataError, match="readie-pipeline analyze"):
        Metadata.load(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# The committed data
# ---------------------------------------------------------------------------
DATA = Path(__file__).parents[2] / "data"


def test_the_committed_corpus_loads():
    corpus = Corpus.load(DATA / "datasets" / "cpu.json")
    metadata = Metadata.load(DATA / "metadata" / "cpu.json")
    assert len(corpus) > 9000
    assert "pandas" in corpus.import_counts(metadata)


def test_every_common_third_party_package_has_metadata():
    # The planner falls back gracefully, but a systematic gap would mean it is
    # scoring most of the corpus on defaults. Stdlib modules are excluded
    # because they install nothing -- and the planner skips them for the same
    # reason.
    #
    # The committed metadata.json predates this file's resolver/closure
    # redesign (no `resolved` map, package keys are top-level names only), so
    # this can only confirm what it always could: a raw import string whose
    # own bare spelling already has metadata resolves via the fallback in
    # `Metadata.resolve_imports`. A raw string that needs real resolution
    # (e.g. a submodule candidate with no exact top-level match) is dropped by
    # that same fallback rather than surfacing here as "missing" -- catching
    # that requires re-running `readie-pipeline analyze` against a real base
    # image with the new resolver, which this suite cannot do.
    import sys

    corpus = Corpus.load(DATA / "datasets" / "cpu.json")
    metadata = Metadata.load(DATA / "metadata" / "cpu.json")

    common = [
        name
        for name, count in corpus.import_counts(metadata).items()
        if count > 50 and name not in sys.stdlib_module_names
    ]
    missing = [name for name in common if name not in metadata]

    assert missing == [], f"no measurements for commonly used packages: {missing}"
