"""Corpus and metadata loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from readie_pipeline.corpus.models import (
    Corpus,
    CorpusError,
    CorpusItem,
    Source,
    parse_resource,
)
from readie_pipeline.metadata.models import Metadata, ResourceFacts
from readie_pipeline.resources.identifiers import ResourceType
from tests.helpers import item


def _metadata_for(*names: str) -> Metadata:
    """Metadata where each of ``names`` has usable facts keyed by itself.

    ``Metadata.resolve_resources`` falls back to a raw string's own name when
    ``resolved`` has nothing for it and that name already has usable facts --
    this is what lets metadata built by hand, without ever running the real
    resolver, still resolve its own plain package names.
    """
    return Metadata({f"package:{name}": ResourceFacts(base_import=name) for name in names})


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
    with pytest.raises(CorpusError, match="gpu"):
        CorpusItem.from_json(_entry(resources=["gpu:a100"]), index=0)


def test_a_resource_without_a_value_is_rejected():
    with pytest.raises(CorpusError, match="nonempty"):
        parse_resource("hf_model:")


def test_resources_are_split_by_kind_and_keep_their_order():
    parsed = CorpusItem.from_json(
        _entry(resources=["package:torch", "hf_model:org/m", "package:pandas", "hf_dataset:imdb"]),
        index=0,
    )
    assert parsed.resources == (
        "package:torch",
        "hf_model:org/m",
        "package:pandas",
        "hf_dataset:imdb",
    )
    assert parsed.of_kind(ResourceType.HF_MODEL) == ("org/m",)
    assert parsed.of_kind(ResourceType.KAGGLE_DATASET) == ()


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
        {
            "package:pandas": ResourceFacts(base_import="pandas"),
            "package:numpy": ResourceFacts(base_import="numpy"),
        },
        resolved={
            "package:pandas": "package:pandas",
            "package:pandas.io": "package:pandas",
            "package:numpy": "package:numpy",
        },
    )
    corpus = Corpus.of([item("pandas", "pandas.io"), item("numpy")])
    assert corpus.resource_counts(metadata) == {"package:numpy": 1, "package:pandas": 1}


def test_import_counts_are_ordered_most_used_first_then_by_name():
    corpus = Corpus.of([item("b"), item("b"), item("a"), item("c")])
    assert list(corpus.resource_counts(_metadata_for("a", "b", "c"))) == [
        "package:b",
        "package:a",
        "package:c",
    ]


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
    resources = corpus.resource_ids()

    assert resources == {
        "package:pandas",
        "package:numpy",
        "hf_dataset:d1",
        "kaggle_dataset:o/d2",
        "hf_model:m1",
        "hf_tokenizer:t1",
        "kaggle_model:o/m2",
    }


def test_an_empty_corpus_has_no_resources():
    assert Corpus.of([]).resource_ids() == frozenset()


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
