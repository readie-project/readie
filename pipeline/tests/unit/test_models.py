"""Corpus and metadata loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from readie_pipeline.corpus.models import (
    Corpus,
    CorpusError,
    CorpusItem,
    ResourceKind,
    Source,
    parse_resource,
)
from readie_pipeline.metadata.models import Metadata, MetadataError, PackageFacts, ResourceType
from tests.helpers import item


def _metadata_for(*names: str) -> Metadata:
    """Metadata where each of ``names`` has usable facts keyed by itself.

    ``Metadata.resolve_imports`` falls back to a raw string's own name when
    ``resolved`` has nothing for it and that name already has usable facts --
    this is what lets metadata built by hand, without ever running the real
    resolver, still resolve its own plain package names.
    """
    return Metadata({name: PackageFacts(base_import=name) for name in names})


def _entry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "code": ["import json"],
        "domain": "Data Science",
        "source": "agent_short",
        "resources": [],
    }
    return {**base, **overrides}


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------
def test_an_item_keeps_one_string_per_step():
    parsed = CorpusItem.from_json(_entry(code=["a = 1", "b = a"]), index=0)
    assert parsed.code == ("a = 1", "b = a")


def test_a_missing_field_names_the_entry_and_the_field():
    with pytest.raises(CorpusError, match="entry 7 is missing code, resources, source"):
        CorpusItem.from_json({"domain": "Data Science"}, index=7)


def test_a_bare_string_where_a_code_list_belongs_is_rejected():
    with pytest.raises(CorpusError, match="list of strings"):
        CorpusItem.from_json(_entry(code="import json"), index=0)


def test_an_item_without_code_is_rejected():
    with pytest.raises(CorpusError, match="no code"):
        CorpusItem.from_json(_entry(code=["  "]), index=3)


def test_an_unknown_domain_is_rejected_by_name():
    with pytest.raises(CorpusError, match="unknown domain 'Astrology'"):
        CorpusItem.from_json(_entry(domain="Astrology"), index=0)


def test_an_unknown_source_is_rejected_by_name():
    with pytest.raises(CorpusError, match="unknown source 'blog'"):
        CorpusItem.from_json(_entry(source="blog"), index=0)


def test_a_resource_of_an_unknown_kind_is_rejected():
    with pytest.raises(CorpusError, match="unknown resource kind"):
        CorpusItem.from_json(_entry(resources=["gpu:a100"]), index=0)


def test_a_resource_without_a_value_is_rejected():
    with pytest.raises(CorpusError, match="no value"):
        parse_resource("hf_model:")


def test_resources_are_split_by_kind_and_keep_their_order():
    parsed = CorpusItem.from_json(
        _entry(resources=["package:torch", "hf_model:org/m", "package:pandas", "hf_dataset:imdb"]),
        index=0,
    )
    assert parsed.imports == ("torch", "pandas")
    assert parsed.of_kind(ResourceKind.HF_MODEL) == ("org/m",)
    assert parsed.of_kind(ResourceKind.KAGGLE_DATASET) == ()


def test_only_agent_sources_are_generated():
    assert [s.value for s in Source if not s.fetched] == [
        "agent_short",
        "agent_notebook",
        "agent_fix",
    ]


def test_import_counts_count_items_not_import_statements():
    # Two import strings that resolve to the same package are one item
    # that needs it, not two -- `pandas` and `pandas.io` both resolving to
    # `pandas` here, exactly as analyze() would record it.
    metadata = Metadata(
        {"pandas": PackageFacts(base_import="pandas"), "numpy": PackageFacts(base_import="numpy")},
        resolved={"pandas": "pandas", "pandas.io": "pandas", "numpy": "numpy"},
    )
    corpus = Corpus.of([item("pandas", "pandas.io"), item("numpy")])
    assert corpus.import_counts(metadata) == {"numpy": 1, "pandas": 1}


def test_import_counts_are_ordered_most_used_first_then_by_name():
    corpus = Corpus.of([item("b"), item("b"), item("a"), item("c")])
    assert list(corpus.import_counts(_metadata_for("a", "b", "c"))) == ["b", "a", "c"]


def test_resources_collects_every_distinct_reference():
    corpus = Corpus.of(
        [
            CorpusItem(
                ("x",),
                "Data Science",
                Source.KAGGLE,
                ("package:pandas", "hf_dataset:d1", "kaggle_dataset:o/d2", "hf_model:m1"),
            ),
            CorpusItem(
                ("y",),
                "Data Science",
                Source.AGENT_SHORT,
                ("package:numpy", "hf_tokenizer:t1", "kaggle_model:o/m2"),
            ),
        ]
    )
    resources = corpus.resources(_metadata_for("pandas", "numpy"))

    assert resources.packages == {"pandas", "numpy"}
    assert resources.datasets == {"d1", "o/d2"}
    assert resources.models == {"m1", "o/m2"}
    assert resources.tokenizers == {"t1"}


def test_an_empty_corpus_has_no_resources():
    assert Corpus.of([]).resources(Metadata({})).packages == frozenset()


def test_counts_by_domain_source_and_pair():
    corpus = Corpus.of(
        [
            item("a", domain="Computer Vision", source=Source.KAGGLE),
            item("b", domain="Computer Vision", source=Source.KAGGLE),
            item("c", domain="Data Science", source=Source.AGENT_FIX),
        ]
    )
    assert corpus.domain_counts() == {"Computer Vision": 2, "Data Science": 1}
    assert corpus.source_counts() == {"kaggle": 2, "agent_fix": 1}
    assert corpus.cell_counts()[("Computer Vision", Source.KAGGLE)] == 2


def test_a_missing_corpus_says_how_to_make_one(tmp_path: Path):
    with pytest.raises(CorpusError, match="readie-pipeline corpus"):
        Corpus.load(tmp_path / "nope.json")


def test_invalid_json_is_named_as_such(tmp_path: Path):
    path = tmp_path / "corpus.json"
    path.write_text("{not json")
    with pytest.raises(CorpusError, match="not valid JSON"):
        Corpus.load(path)


def test_a_corpus_round_trips_through_disk(tmp_path: Path):
    original = Corpus.of(
        [
            CorpusItem(
                ("a = 1", "b = 2"),
                "Computer Vision",
                Source.KAGGLE_NOTEBOOK,
                ("package:torch",),
                ref="https://www.kaggle.com/code/o/n",
            )
        ]
    )
    path = tmp_path / "nested" / "corpus.json"
    original.save(path)

    assert Corpus.load(path).items == original.items
    assert not path.with_suffix(".json.partial").exists()


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
