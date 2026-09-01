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


def test_imports_are_reduced_to_their_top_level_package():
    # The planner reasons about what must be installed, which is the
    # distribution-level name.
    r = request(imports=("sklearn.svm", "sklearn.metrics", "numpy"))
    assert r.top_level_imports == {"sklearn", "numpy"}


def test_a_string_where_a_list_belongs_is_rejected():
    with pytest.raises(CorpusError, match="list of strings"):
        Request.from_json(
            {"task_name": "t", "category": "c", "code": "", "imports": "numpy"}, index=0
        )


def test_import_counts_count_requests_not_import_statements():
    # Two `import pandas` lines in one snippet are one request that needs pandas.
    corpus = Corpus.of([request(imports=("pandas", "pandas.io")), request(imports=("numpy",))])
    assert corpus.import_counts() == {"numpy": 1, "pandas": 1}


def test_import_counts_are_ordered_most_used_first_then_by_name():
    corpus = Corpus.of(
        [
            request(imports=("b",)),
            request(imports=("b",)),
            request(imports=("a",)),
            request(imports=("c",)),
        ]
    )
    assert list(corpus.import_counts()) == ["b", "a", "c"]


def test_resources_collects_every_distinct_reference():
    corpus = Corpus.of(
        [
            request(imports=("pandas",), datasets=("d1",), models=("m1",)),
            request(imports=("numpy",), tokenizers=("t1",)),
        ]
    )
    resources = corpus.resources()

    assert resources.packages == {"pandas", "numpy"}
    assert resources.datasets == {"d1"}
    assert resources.models == {"m1"}
    assert resources.tokenizers == {"t1"}


def test_an_empty_corpus_has_no_resources():
    assert Corpus.of([]).resources().packages == frozenset()


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
    # builtin -- so the field never appeared in any of the 918 entries.
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


def test_metadata_round_trips_and_sorts_for_a_clean_diff(tmp_path: Path):
    metadata = Metadata(
        {
            "zlib": PackageFacts(base_import="zlib", import_time=0.1),
            "abc": PackageFacts(base_import="abc", import_time=0.2),
        }
    )
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(metadata.to_json()))

    assert list(json.loads(path.read_text())) == ["abc", "zlib"]
    assert Metadata.load(path).import_time("abc") == 0.2


def test_a_missing_metadata_file_says_how_to_make_one(tmp_path: Path):
    with pytest.raises(MetadataError, match="readie-pipeline analyze"):
        Metadata.load(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# The committed data
# ---------------------------------------------------------------------------
DATA = Path(__file__).parents[2] / "data"


def test_the_committed_corpus_loads():
    corpus = Corpus.load(DATA / "dataset.json")
    assert len(corpus) > 9000
    assert "pandas" in corpus.import_counts()


def test_the_committed_metadata_loads_and_has_real_measurements():
    metadata = Metadata.load(DATA / "metadata.json")
    assert len(metadata) > 900
    assert metadata.import_time("pandas") > 0
    assert metadata.size_mb("pandas") > 0


def test_every_common_third_party_package_has_metadata():
    # The planner falls back gracefully, but a systematic gap would mean it is
    # scoring most of the corpus on defaults. Stdlib modules are excluded
    # because they install nothing -- and the planner skips them for the same
    # reason.
    import sys

    corpus = Corpus.load(DATA / "dataset.json")
    metadata = Metadata.load(DATA / "metadata.json")

    common = [
        name
        for name, count in corpus.import_counts().items()
        if count > 50 and name not in sys.stdlib_module_names
    ]
    missing = [name for name in common if name not in metadata]

    assert missing == [], f"no measurements for commonly used packages: {missing}"
