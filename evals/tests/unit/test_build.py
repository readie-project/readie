from __future__ import annotations

from readie_evals.agent._build import build_session_task, build_task


def _payload(code, **extra):
    return {"code": code, **extra}


def _session_payload(*cells, **extra):
    return {"cells": list(cells), **extra}


def test_accepts_a_valid_task_and_records_created_at():
    task = build_task(
        source="generated",
        category="Machine Learning",
        payload=_payload("def task():\n    return 1\n"),
        warm=frozenset(),
    )
    assert task is not None
    assert task.entrypoint == "task"
    assert task.created_at != ""


def test_infers_missing_packages_from_imports():
    task = build_task(
        source="generated",
        category="ML",
        payload=_payload("def task():\n    import sklearn\n    return 1\n"),
        warm=frozenset(),
    )
    assert task is not None
    assert task.packages == ("scikit-learn",)


def test_warm_imports_need_no_package():
    task = build_task(
        source="generated",
        category="ML",
        payload=_payload("def task():\n    import numpy\n    return 1\n"),
        warm=frozenset({"numpy"}),
    )
    assert task is not None
    assert task.packages == ()


def test_rejects_disallowed_framework():
    task = build_task(
        source="generated",
        category="CV",
        payload=_payload("def task():\n    import torch\n    return 1\n"),
        warm=frozenset(),
    )
    assert task is None


def test_rejects_code_without_the_entrypoint():
    task = build_task(
        source="generated",
        category="ML",
        payload=_payload("def other():\n    return 1\n"),
        warm=frozenset(),
    )
    assert task is None


def test_rejects_unparsable_code():
    task = build_task(
        source="generated",
        category="ML",
        payload=_payload("def task(:\n"),
        warm=frozenset(),
    )
    assert task is None


def test_rejects_payload_without_code():
    assert build_task(source="x", category="y", payload={}, warm=frozenset()) is None


def test_build_session_task_unions_cells_into_one_task():
    task = build_session_task(
        source="generated-notebook",
        category="ML",
        payload=_session_payload(
            "def task():\n    import numpy\n    return 1\n",
            "def task():\n    import sklearn\n    return 2\n",
        ),
        warm=frozenset({"numpy"}),
        kind="notebook",
    )
    assert task is not None
    assert task.code == (
        "def task():\n    import numpy\n    return 1",
        "def task():\n    import sklearn\n    return 2",
    )
    assert task.session_kind == "notebook"
    assert set(task.imports) == {"numpy", "sklearn"}
    assert task.packages == ("scikit-learn",)


def test_build_session_task_drops_the_whole_session_on_one_bad_cell():
    task = build_session_task(
        source="generated-retry",
        category="ML",
        payload=_session_payload("def task():\n    return 1\n", "def other():\n    return 1\n"),
        warm=frozenset(),
        kind="retry",
    )
    assert task is None


def test_rejects_session_payload_without_cells():
    assert (
        build_session_task(source="x", category="y", payload={}, warm=frozenset(), kind="notebook")
        is None
    )
