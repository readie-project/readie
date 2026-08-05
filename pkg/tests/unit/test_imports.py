"""Static import extraction from a decorated function."""

from __future__ import annotations

from crfs._ast import analyse
from crfs.resources import extract_imports


def test_imports_are_extracted_from_the_function_body() -> None:
    def uses_libraries():
        import json
        import statistics
        from collections import OrderedDict

        return json, statistics, OrderedDict

    names = set(extract_imports(uses_libraries))

    assert {"json", "statistics", "collections"} <= names


def test_aliases_report_the_real_module_name() -> None:
    facts = analyse("import numpy as np\nimport pandas as pd")
    assert facts.imports == {"numpy", "pandas"}


def test_relative_imports_are_skipped_because_they_name_no_module() -> None:
    facts = analyse("from . import sibling\nfrom .pkg import thing")
    assert facts.imports == set()


def test_from_pretrained_models_and_tokenizers_are_distinguished() -> None:
    facts = analyse(
        "tok = AutoTokenizer.from_pretrained('bert-base-uncased')\n"
        "mdl = AutoModel.from_pretrained(pretrained_model_name_or_path='gpt2')\n"
    )
    assert facts.tokenizers == {"bert-base-uncased"}
    assert facts.models == {"gpt2"}


class AutoModel:
    """Stands in for the transformers class; only its *name* is analysed."""

    @staticmethod
    def from_pretrained(name: str) -> str:
        return name


def test_models_ride_along_as_prefixed_imports() -> None:
    def loads_a_model():
        return AutoModel.from_pretrained("gpt2")

    assert "model:gpt2" in extract_imports(loads_a_model)


def test_a_function_with_no_retrievable_source_extracts_nothing() -> None:
    # exec'd code has no source file; a REPL definition is the real-world case.
    namespace: dict[str, object] = {}
    exec("def dynamic():\n    import os\n    return os", namespace)  # noqa: S102

    assert extract_imports(namespace["dynamic"]) == ()  # type: ignore[arg-type]


def test_a_builtin_extracts_nothing_rather_than_raising() -> None:
    assert extract_imports(len) == ()


def test_extraction_is_cached_per_function() -> None:
    def target():
        import json

        return json

    first = extract_imports(target)
    assert extract_imports(target) is first
