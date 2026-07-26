"""AST estimation of a decorated function."""

from __future__ import annotations

from crfs._ast import analyse
from crfs.resources import EMPTY_ESTIMATE, AstEstimator, NullEstimator


def test_imports_are_extracted_from_the_function_body() -> None:
    def uses_libraries():
        import json
        import statistics
        from collections import OrderedDict

        return json, statistics, OrderedDict

    estimate = AstEstimator().estimate(uses_libraries)
    names = {i.name for i in estimate.imports}

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

    estimate = AstEstimator().estimate(loads_a_model)
    assert any(i.id == "model:gpt2" for i in estimate.imports)


def test_assignments_become_variables_with_a_guessed_type() -> None:
    def binds():
        count = 3
        label = "x"
        rows = [1, 2]
        return count, label, rows

    variables = {v.id: v.type for v in AstEstimator().estimate(binds).variables}
    assert variables == {"count": "int", "label": "str", "rows": "list"}


def test_an_annotated_assignment_prefers_the_written_annotation() -> None:
    facts = analyse("x: numbers.Real = 1")
    assert facts.assignments["x"] == "numbers.Real"


def test_a_function_with_no_retrievable_source_estimates_to_nothing() -> None:
    # exec'd code has no source file; a REPL definition is the real-world case.
    namespace: dict[str, object] = {}
    exec("def dynamic():\n    import os\n    return os", namespace)  # noqa: S102

    estimate = AstEstimator().estimate(namespace["dynamic"])  # type: ignore[arg-type]
    assert estimate.imports == ()
    assert estimate.code == ""


def test_a_builtin_estimates_to_nothing_rather_than_raising() -> None:
    assert AstEstimator().estimate(len).imports == ()


def test_estimation_is_cached_per_function() -> None:
    estimator = AstEstimator()

    def target():
        import json

        return json

    first = estimator.estimate(target)
    assert estimator.estimate(target) is first


def test_include_source_false_omits_the_body() -> None:
    def target():
        import json

        return json

    assert AstEstimator(include_source=False).estimate(target).code == ""
    assert AstEstimator().estimate(target).code != ""


def test_the_null_estimator_sends_nothing_even_for_analysable_source() -> None:
    def target():
        import json

        return json

    assert AstEstimator().estimate(target).imports  # the source is analysable
    assert NullEstimator().estimate(target) == EMPTY_ESTIMATE
