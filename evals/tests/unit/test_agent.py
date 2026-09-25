from __future__ import annotations

import json

from readie_evals.agent.adapt import adapt_candidate, adapt_session_candidate
from readie_evals.agent.generate import generate_session_tasks, generate_tasks
from readie_evals.agent.repair import repair_cell
from readie_evals.models import RawCandidate
from tests.fakes.llm import FakeLLM

_GOOD = "def task():\n    return sum(range(5))\n"
_GOOD2 = "def task():\n    return sum(range(6))\n"


def _snippets_response(*codes):
    return json.dumps({"snippets": [{"code": c, "packages": [], "memory": None} for c in codes]})


def _task_response(code):
    return json.dumps({"code": code, "packages": [], "memory": None})


def _sessions_response(*cell_lists):
    sessions = [{"cells": list(cells), "packages": [], "memory": None} for cells in cell_lists]
    return json.dumps({"sessions": sessions})


def test_generate_tasks_builds_valid_snippets():
    llm = FakeLLM([_snippets_response(_GOOD, "def task():\n    return 2\n")])
    tasks = generate_tasks(
        llm, category="Machine Learning", count=2, warm=frozenset(), on_line=lambda _line: None
    )
    assert len(tasks) == 2
    assert all(t.source == "generated" for t in tasks)


def test_generate_tasks_skips_unusable_snippets():
    llm = FakeLLM([_snippets_response("def nope():\n    return 1\n")])
    tasks = generate_tasks(
        llm, category="ML", count=1, warm=frozenset(), on_line=lambda _line: None
    )
    assert tasks == []


def test_generate_tasks_handles_a_non_json_response():
    tasks = generate_tasks(
        FakeLLM(["not json"]), category="ML", count=1, warm=frozenset(), on_line=lambda _line: None
    )
    assert tasks == []


def test_adapt_candidate_tags_the_source_slug_and_provenance():
    candidate = RawCandidate(
        source="kaggle",
        category="Data Science",
        raw_code="print('messy notebook code')",
        slug="someuser/some-kernel",
        provenance="https://kaggle.com/code/x",
    )
    task = adapt_candidate(FakeLLM([_task_response(_GOOD)]), candidate, warm=frozenset())
    assert task is not None
    assert task.source == "kaggle"
    assert task.slug == "someuser/some-kernel"
    assert task.provenance == "https://kaggle.com/code/x"


def test_adapt_session_candidate_builds_a_multi_cell_notebook_from_real_cells():
    candidate = RawCandidate(
        source="kaggle",
        category="Data Science",
        raw_code="print(1)\n\nprint(2)",
        raw_cells=("print(1)", "print(2)"),
        slug="someuser/some-notebook",
        provenance="https://kaggle.com/code/someuser/some-notebook",
    )
    response = json.dumps({"cells": [_GOOD, _GOOD2], "packages": [], "memory": None})
    task = adapt_session_candidate(FakeLLM([response]), candidate, warm=frozenset())
    assert task is not None
    assert task.code == (_GOOD.strip(), _GOOD2.strip())
    assert task.session_kind == "notebook"
    assert task.source == "kaggle"
    assert task.slug == "someuser/some-notebook"
    assert task.provenance == "https://kaggle.com/code/someuser/some-notebook"


def test_adapt_session_candidate_skips_a_single_cell_candidate():
    candidate = RawCandidate(
        source="huggingface",
        category="ML",
        raw_code="print(1)",
        raw_cells=("print(1)",),
    )
    task = adapt_session_candidate(FakeLLM([]), candidate, warm=frozenset())
    assert task is None


def test_repair_cell_returns_corrected_code_and_packages():
    fix = repair_cell(
        FakeLLM([_task_response(_GOOD)]),
        category="ML",
        code="def task():\n    return undefined\n",
        error_type="NameError",
        error_message="name 'undefined' is not defined",
        warm=frozenset(),
    )
    assert fix is not None
    code, packages = fix
    assert code == _GOOD.strip()
    assert packages == ()


def test_repair_cell_infers_a_missing_package():
    response = json.dumps(
        {"code": "def task():\n    import sklearn\n    return 1\n", "packages": []}
    )
    fix = repair_cell(
        FakeLLM([response]),
        category="ML",
        code="def task():\n    return undefined\n",
        error_type="NameError",
        error_message="name 'undefined' is not defined",
        warm=frozenset(),
    )
    assert fix is not None
    _code, packages = fix
    assert packages == ("scikit-learn",)


def test_repair_cell_returns_none_on_a_non_json_response():
    fix = repair_cell(
        FakeLLM(["not json"]),
        category="ML",
        code="def task():\n    return undefined\n",
        error_type="NameError",
        error_message="name 'undefined' is not defined",
        warm=frozenset(),
    )
    assert fix is None


def test_generate_session_tasks_builds_a_multi_cell_notebook():
    llm = FakeLLM([_sessions_response([_GOOD, _GOOD2])])
    tasks = generate_session_tasks(
        llm,
        category="Machine Learning",
        count=1,
        warm=frozenset(),
        kind="notebook",
        on_line=lambda _line: None,
    )
    assert len(tasks) == 1
    task = tasks[0]
    assert task.code == (_GOOD.strip(), _GOOD2.strip())
    assert task.session_kind == "notebook"
    assert task.source == "generated-notebook"
    assert task.is_session is True


def test_generate_session_tasks_skips_a_session_with_one_bad_cell():
    llm = FakeLLM([_sessions_response([_GOOD, "def nope():\n    return 1\n"])])
    tasks = generate_session_tasks(
        llm,
        category="ML",
        count=1,
        warm=frozenset(),
        kind="retry",
        on_line=lambda _line: None,
    )
    assert tasks == []
