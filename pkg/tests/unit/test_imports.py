"""Static import extraction from a decorated function."""

from __future__ import annotations

import statistics as stats
import uuid

from readie._ast import analyse
from readie.resources import extract_imports


def test_imports_are_extracted_from_the_function_body() -> None:
    def uses_libraries():
        import json
        import statistics
        from collections import OrderedDict

        return json, statistics, OrderedDict

    names = set(extract_imports(uses_libraries))

    assert {"json", "statistics", "collections.OrderedDict"} <= names


def test_aliases_report_the_real_module_name() -> None:
    facts = analyse("import numpy as np\nimport pandas as pd")
    assert facts.imports == {"numpy", "pandas"}


def test_relative_imports_are_skipped_because_they_name_no_module() -> None:
    facts = analyse("from . import sibling\nfrom .pkg import thing")
    assert facts.imports == set()


def test_a_from_import_records_the_deepest_candidate() -> None:
    # `Image` might be a real submodule (PIL.Image is) or just an attribute of
    # the module (sklearn.svm.LinearSVC is a class, not a submodule) -- that
    # can only be told apart by actually importing it, which this AST-only
    # pass cannot do. So it records the deepest candidate implied by the
    # syntax and leaves resolving it to whatever runs in the target
    # environment. Must match `pipeline`'s `corpus/tree_parser.py` exactly --
    # see the module docstring's sync note.
    facts = analyse("from PIL import Image\nfrom sklearn.svm import LinearSVC\n")
    assert facts.imports == {"PIL.Image", "sklearn.svm.LinearSVC"}


def test_a_from_import_with_several_names_records_each_candidate() -> None:
    facts = analyse("from os.path import join, exists\n")
    assert facts.imports == {"os.path.join", "os.path.exists"}


def test_a_star_import_records_just_the_module() -> None:
    # There is no name to append a candidate onto.
    facts = analyse("from os.path import *\n")
    assert facts.imports == {"os.path"}


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


def test_a_module_imported_at_module_scope_is_resolved_via_global_lookup() -> None:
    def generates_an_id():
        return uuid.uuid4()

    assert "uuid" in extract_imports(generates_an_id)


def test_an_aliased_module_level_import_reports_the_real_name() -> None:
    def computes_mean():
        return stats.mean([1, 2, 3])

    assert "statistics" in extract_imports(computes_mean)


def test_a_module_imported_in_an_enclosing_function_is_resolved_via_closure() -> None:
    def outer():
        import json as _json

        def inner():
            return _json.dumps({})

        return inner

    assert "json" in extract_imports(outer())


def _local_helper() -> str:
    return "helper"


def test_a_sibling_defined_in_the_same_module_adds_no_import() -> None:
    def calls_helper():
        return _local_helper()

    assert __name__ not in extract_imports(calls_helper)


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


def test_a_garbage_collected_function_leaves_no_stale_cache_entry() -> None:
    # Pins a real bug: the cache used to key on id(func) alone, with no
    # reference keeping func alive. Once a function was garbage collected,
    # CPython was free to hand that same id to an unrelated function defined
    # later, which then got back the first function's cached imports instead
    # of its own. Keying on the function object itself, in a WeakKeyDictionary,
    # means the entry is dropped instead of left behind for something else to
    # collide with.
    import gc

    from readie import resources

    def target():
        import json

        return json

    extract_imports(target)
    before = len(resources._cache)

    del target
    gc.collect()

    # Strictly fewer, not exactly one fewer: other closures from earlier tests
    # in this same process may also be swept by this collection. The old
    # dict-keyed-by-id cache never shrank at all -- entries lived forever -- so
    # any decrease here is proof the fix actually drops dead entries rather
    # than leaving their id free for something else to collide with.
    assert len(resources._cache) < before
