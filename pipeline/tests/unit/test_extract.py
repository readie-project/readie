import pytest

from readie_pipeline.corpus.extract import CodeError, clean_cell, extract_resources


def test_a_from_import_records_the_deepest_candidate_like_the_sdk_does():
    # Must match pkg/src/readie/_ast.py: the router compares these strings with
    # a live request's imports using plain equality.
    code = [
        "import os.path\nimport pandas as pd\n"
        "from sklearn.svm import LinearSVC\nfrom os.path import *"
    ]
    assert extract_resources(code) == (
        "package:os.path",
        "package:pandas",
        "package:sklearn.svm.LinearSVC",
    )


def test_the_standard_library_is_kept_because_the_sdk_keeps_it():
    assert extract_resources(["import json"]) == ("package:json",)


def test_relative_imports_are_not_resources():
    assert extract_resources(["from . import helpers\nfrom .utils import x"]) == ()


def test_from_pretrained_names_a_model_or_a_tokenizer_by_the_class_called():
    code = [
        "from transformers import AutoTokenizer, AutoModel\n"
        "tok = AutoTokenizer.from_pretrained('bert-base-uncased')\n"
        "model = AutoModel.from_pretrained('google/vit-base-patch16-224')"
    ]
    found = extract_resources(code)
    assert "hf_tokenizer:bert-base-uncased" in found
    assert "hf_model:google/vit-base-patch16-224" in found


def test_a_local_path_to_from_pretrained_is_not_a_hub_resource():
    assert "hf_model:./ckpt" not in extract_resources(
        ["m = AutoModel.from_pretrained('./ckpt')\nn = AutoModel.from_pretrained('/mnt/m')"]
    )


def test_pipeline_model_keyword_is_a_model():
    found = extract_resources(["p = pipeline('sentiment-analysis', model='org/sentiment')"])
    assert "hf_model:org/sentiment" in found


def test_load_dataset_names_a_dataset_but_a_builder_is_not_one():
    found = extract_resources(
        ["a = load_dataset('imdb')\nb = load_dataset('csv', data_files='x.csv')"]
    )
    assert "hf_dataset:imdb" in found
    assert "hf_dataset:csv" not in found


def test_kagglehub_downloads_name_kaggle_resources():
    code = [
        "p = kagglehub.dataset_download('owner/titanic')\n"
        "m = kagglehub.model_download('google/gemma/pyTorch/2b')"
    ]
    found = extract_resources(code)
    assert "kaggle_dataset:owner/titanic" in found
    assert "kaggle_model:google/gemma" in found


def test_resources_are_gathered_across_every_step_without_repeats():
    found = extract_resources(["import numpy", "import numpy\nimport pandas"])
    assert found == ("package:numpy", "package:pandas")


def test_notebook_magics_and_shell_lines_do_not_break_parsing():
    code = ["%matplotlib inline\n!pip install seaborn\nimport seaborn as sns"]
    assert extract_resources(code) == ("package:seaborn",)
    assert clean_cell("!ls\nx = 1") == "x = 1"


def test_code_that_is_not_python_is_an_error():
    with pytest.raises(CodeError):
        extract_resources(["def broken(:"])


def test_invalid_escape_sequences_parse_without_emitting_a_warning():
    # pytest runs with filterwarnings=error, so a leaked SyntaxWarning fails this.
    assert extract_resources(["import re\npattern = re.compile('\\d+\\.\\d+')\nx = '\\s'"]) == (
        "package:re",
    )
