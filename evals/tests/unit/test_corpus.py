from __future__ import annotations

from readie_evals.corpus import CorpusStore
from readie_evals.models import Task


def _task(code, source="generated", category="ML"):
    return Task(source=source, category=category, code=(code,))


def test_append_dedupes_by_id(tmp_path):
    store = CorpusStore(tmp_path / "corpus.jsonl")
    added = store.append([_task("def task():\n    return 1\n")])
    again = store.append([_task("def task():\n    return 1\n")])
    assert added == 1
    assert again == 0
    assert len(store.load()) == 1


def test_counts_group_by_source_and_category(tmp_path):
    store = CorpusStore(tmp_path / "corpus.jsonl")
    store.append(
        [
            _task("def task():\n    return 1\n", source="kaggle"),
            _task("def task():\n    return 2\n", source="kaggle"),
            _task("def task():\n    return 3\n", source="generated"),
        ],
    )
    counts = store.counts()
    assert counts[("kaggle", "ML")] == 2
    assert counts[("generated", "ML")] == 1


def test_deficit_reports_only_the_shortfall(tmp_path):
    store = CorpusStore(tmp_path / "corpus.jsonl")
    store.append([_task("def task():\n    return 1\n", source="kaggle")])
    deficit = store.deficit({("kaggle", "ML"): 3, ("generated", "ML"): 2})
    assert deficit == {("kaggle", "ML"): 2, ("generated", "ML"): 2}


def test_deficit_omits_satisfied_cells(tmp_path):
    store = CorpusStore(tmp_path / "corpus.jsonl")
    store.append([_task("def task():\n    return 1\n", source="kaggle")])
    assert store.deficit({("kaggle", "ML"): 1}) == {}


def test_load_survives_a_reopen(tmp_path):
    path = tmp_path / "corpus.jsonl"
    CorpusStore(path).append([_task("def task():\n    return 1\n")])
    assert len(CorpusStore(path).load()) == 1
