"""Static analysis of corpus snippets."""

from __future__ import annotations

import pytest

from readie_pipeline.corpus.tree_parser import analyse, clean


def test_imports_are_extracted():
    facts = analyse("import numpy as np\nfrom collections import OrderedDict\n")
    assert facts.imports == {"numpy", "collections"}


def test_relative_imports_are_skipped_because_they_name_no_module():
    assert analyse("from . import sibling\nfrom .pkg import thing").imports == set()


def test_notebook_magics_do_not_break_parsing():
    # Snippets imitate notebook code, so they carry magics, shell escapes and
    # help queries. None of that is Python, and ast.parse rejects the whole file
    # over one line of it.
    code = "%matplotlib inline\n!pip install pandas\n?DataFrame\nimport pandas\n"
    assert analyse(code).imports == {"pandas"}


def test_an_indented_magic_is_also_stripped():
    assert analyse("def f():\n    %time x = 1\n    import json\n").imports == {"json"}


def test_the_dataset_marker_is_collected_and_the_comment_dropped():
    code = "# DATASET USED: https://kaggle.com/x/y\nimport pandas\n"
    facts = analyse(code)

    assert facts.datasets == {"https://kaggle.com/x/y"}
    assert facts.imports == {"pandas"}


def test_an_empty_dataset_marker_is_ignored():
    assert analyse("# DATASET USED:\nimport json").datasets == set()


def test_ordinary_comments_are_dropped_without_becoming_datasets():
    assert analyse("# just a comment\nimport json").datasets == set()


def test_from_pretrained_distinguishes_models_from_tokenizers():
    code = (
        "tok = AutoTokenizer.from_pretrained('bert-base-uncased')\n"
        "mdl = AutoModel.from_pretrained(pretrained_model_name_or_path='gpt2')\n"
    )
    facts = analyse(code)

    assert facts.tokenizers == {"bert-base-uncased"}
    assert facts.models == {"gpt2"}


def test_a_pipeline_class_counts_as_a_model():
    assert analyse("p = StableDiffusionPipeline.from_pretrained('sd')").models == {"sd"}


def test_from_pretrained_without_a_literal_name_is_ignored():
    assert analyse("m = AutoModel.from_pretrained(name)").models == set()


def test_clean_returns_parsable_source():
    source, datasets = clean("%magic\nx = 1\n# DATASET USED: d\n")
    assert source.strip() == "x = 1"
    assert datasets == {"d"}


def test_source_that_is_still_unparsable_raises():
    # The caller decides: the corpus generator skips the snippet and says which,
    # rather than failing the whole batch.
    with pytest.raises(SyntaxError):
        analyse("def broken(:\n")


def test_facts_serialise_sorted_so_a_regenerated_corpus_diffs_cleanly():
    facts = analyse("import zlib\nimport abc\n")
    assert facts.to_json()["imports"] == ["abc", "zlib"]
