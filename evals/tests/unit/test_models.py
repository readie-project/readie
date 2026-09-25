from __future__ import annotations

import pytest

from readie_evals.models import Task, TaskError, make_task_id


def _task(**overrides) -> Task:
    base = {
        "source": "generated",
        "category": "Machine Learning",
        "code": ("def task():\n    return 1\n",),
    }
    return Task(**{**base, **overrides})


def test_task_id_is_stable_across_equal_source_and_code():
    assert _task().id == make_task_id("generated", ("def task():\n    return 1\n",))


def test_task_id_changes_with_code():
    assert _task().id != _task(code=("def task():\n    return 2\n",)).id


def test_is_session_reflects_the_number_of_cells():
    assert _task().is_session is False
    assert _task(code=("a", "b")).is_session is True


def test_json_round_trip_preserves_fields():
    task = _task(
        imports=("numpy",),
        packages=("scikit-learn",),
        memory="4Gi",
        timeout=30.0,
        slug="someuser/some-kernel",
        provenance="https://www.kaggle.com/code/someuser/some-kernel",
    )
    restored = Task.from_json(task.to_json(), index=0)
    assert restored == task


def test_from_json_names_missing_fields():
    with pytest.raises(TaskError, match="missing category, code"):
        Task.from_json({"source": "kaggle"}, index=3)


def test_from_json_rejects_a_string_where_a_list_is_expected():
    raw = {"source": "generated", "category": "x", "code": ["y"], "imports": "numpy"}
    with pytest.raises(TaskError, match="list of strings"):
        Task.from_json(raw, index=0)


def test_from_json_rejects_a_bare_string_code():
    raw = {"source": "generated", "category": "x", "code": "y"}
    with pytest.raises(TaskError, match="non-empty list of strings"):
        Task.from_json(raw, index=0)


def test_from_json_rejects_an_unknown_session_kind():
    raw = {"source": "generated", "category": "x", "code": ["y"], "session_kind": "loop"}
    with pytest.raises(TaskError, match="session_kind"):
        Task.from_json(raw, index=0)
