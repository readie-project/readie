"""Re-deriving corpus facts from already-generated code."""

from __future__ import annotations

import json
from pathlib import Path

from readie_pipeline.corpus.generate import reparse, reparse_requests
from readie_pipeline.corpus.models import Corpus, Request


def request(**kwargs: object) -> Request:
    base: dict[str, object] = {"task_name": "t", "category": "c", "code": ""}
    return Request(**{**base, **kwargs})  # type: ignore[arg-type]


def test_reparse_recomputes_imports_from_the_existing_code():
    # Standing in for a real case: the corpus was generated before
    # tree_parser.py recorded the deepest candidate for a `from` import, so
    # its stored `imports` is stale relative to what re-parsing the same
    # code produces now.
    stale = request(code="from sklearn.svm import LinearSVC\n", imports=("sklearn.svm",))

    reparsed = reparse_requests([stale])

    assert reparsed[0].imports == ("sklearn.svm.LinearSVC",)


def test_reparse_also_refreshes_datasets_models_and_tokenizers():
    code = (
        "# DATASET USED: https://kaggle.com/x/y\nimport pandas\nAutoModel.from_pretrained('gpt2')\n"
    )
    stale = request(code=code, imports=(), datasets=(), models=())

    reparsed = reparse_requests([stale])

    assert reparsed[0].imports == ("pandas",)
    assert reparsed[0].datasets == ("https://kaggle.com/x/y",)
    assert reparsed[0].models == ("gpt2",)


def test_reparse_preserves_task_name_and_category():
    stale = request(task_name="my_task", category="my_category", code="import json")
    reparsed = reparse_requests([stale])
    assert reparsed[0].task_name == "my_task"
    assert reparsed[0].category == "my_category"


def test_a_request_that_no_longer_parses_is_kept_as_is_not_dropped():
    # Should not happen in practice (the same source parsed fine when the
    # corpus was first generated), but silently shrinking the corpus over it
    # would be worse than keeping the stale facts and surfacing the warning.
    broken = request(code="def broken(:\n", imports=("something",))
    skipped: list[tuple[str, str]] = []

    reparsed = reparse_requests([broken], on_skip=lambda name, why: skipped.append((name, why)))

    assert reparsed == [broken]
    assert skipped == [("t", skipped[0][1])]
    assert "SyntaxError" in skipped[0][1]


def test_reparse_writes_the_corpus_back_to_disk(tmp_path: Path):
    corpus = Corpus.of(
        [
            request(
                task_name="a", code="from sklearn.svm import LinearSVC\n", imports=("sklearn.svm",)
            )
        ]
    )
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps([r.to_json() for r in corpus]))

    updated = reparse(path, on_line=lambda _: None)

    assert updated.requests[0].imports == ("sklearn.svm.LinearSVC",)
    assert Corpus.load(path).requests[0].imports == ("sklearn.svm.LinearSVC",)


def test_reparse_reports_how_many_requests_it_touched(tmp_path: Path):
    corpus = Corpus.of([request(task_name="a"), request(task_name="b")])
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps([r.to_json() for r in corpus]))

    lines: list[str] = []
    reparse(path, on_line=lines.append)

    assert any("2 requests" in line for line in lines)
