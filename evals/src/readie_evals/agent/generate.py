"""Self-generated workloads: the model writing fresh code for a domain.

The third source. Unlike adaptation, there is no fetched snippet -- the model is
asked directly for realistic code in a category, then the same validator turns each
into a task.
"""

from __future__ import annotations

from collections.abc import Callable

from readie_evals.agent._build import build_session_task, build_task, contract, session_contract
from readie_evals.agent._json import ResponseError, extract_json
from readie_evals.agent.ports import LLM
from readie_evals.models import Task

_SYSTEM = """\
You are a senior data/ML engineer writing realistic, idiomatic Python that a \
practitioner would actually run for a given task. Vary the approach and libraries \
across requests. Follow the task contract exactly."""

_SESSION_SYSTEM = {
    "notebook": (
        "You are a senior data/ML engineer writing a realistic Jupyter-notebook-style "
        "Python session: a sequence of cells that build on each other, the way a "
        "practitioner would actually work through a task interactively. Vary the "
        "approach and libraries across requests. Follow the task contract exactly."
    ),
    "retry": (
        "You are a senior data/ML engineer iterating on a Python task inside one live "
        "session, the way a practitioner revises code after seeing it fail or "
        "reviewing it. Vary the approach and libraries across requests. Follow the "
        "task contract exactly."
    ),
}


def generate_tasks(
    llm: LLM,
    *,
    category: str,
    count: int,
    warm: frozenset[str],
    on_line: Callable[[str], None] = print,
) -> list[Task]:
    """Ask the model for ``count`` fresh tasks in ``category``.

    Returns the ones that validate; a snippet that will not compile or breaks a
    rule is dropped with a logged reason rather than failing the batch.
    """
    user = (
        f"Write {count} distinct, realistic Python snippets for the category: "
        f"{category}.\n\n{contract(warm)}\n\n"
        f'Return a JSON object {{"snippets": [<one task object per snippet>]}}.'
    )
    try:
        payload = extract_json(llm.complete(system=_SYSTEM, user=user))
    except ResponseError as exc:
        on_line(f"    generate {category}: {exc}")
        return []

    entries = payload.get("snippets") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        on_line(f"    generate {category}: response had no 'snippets' list")
        return []

    tasks: list[Task] = []
    for entry in entries:
        task = build_task(source="generated", category=category, payload=entry, warm=warm)
        if task is None:
            on_line(f"    generate {category}: skipped an unusable snippet")
        else:
            tasks.append(task)
    return tasks


def generate_session_tasks(
    llm: LLM,
    *,
    category: str,
    count: int,
    warm: frozenset[str],
    kind: str,
    cells: int = 3,
    on_line: Callable[[str], None] = print,
) -> list[Task]:
    """Ask the model for ``count`` fresh multi-cell session tasks of ``kind``.

    ``kind`` is ``"notebook"`` (cells build on state an earlier one left behind) or
    ``"retry"`` (each cell is an independent, improved attempt at the same goal).
    Returns the sessions that validate; one that will not compile or breaks a rule
    is dropped with a logged reason rather than failing the batch.
    """
    user = (
        f"Write {count} distinct, realistic {kind} sessions for the category: "
        f"{category}.\n\n{session_contract(warm, kind=kind, cells=cells, category=category)}\n\n"
        f'Return a JSON object {{"sessions": [<one session object per session>]}}.'
    )
    try:
        payload = extract_json(llm.complete(system=_SESSION_SYSTEM[kind], user=user))
    except ResponseError as exc:
        on_line(f"    generate {category} ({kind}): {exc}")
        return []

    entries = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        on_line(f"    generate {category} ({kind}): response had no 'sessions' list")
        return []

    tasks: list[Task] = []
    for entry in entries:
        task = build_session_task(
            source=f"generated-{kind}",
            category=category,
            payload=entry,
            warm=warm,
            kind=kind,
        )
        if task is None:
            on_line(f"    generate {category} ({kind}): skipped an unusable session")
        else:
            tasks.append(task)
    return tasks
