"""Adapting fetched code into a runnable task.

Real Kaggle and HuggingFace code is messy: notebook magics, GPU frameworks,
authenticated or competition-only dataset reads, no clean entrypoint. The agent
rewrites one candidate into a self-contained CPU-only function (or, for a candidate
that preserves real notebook cells, a multi-cell session) that keeps the spirit of
the original -- including using a real dataset, swapped for a public, no-auth
equivalent when the original needed credentials -- but runs in the sandbox. The
shared validator turns the result into a task.
"""

from __future__ import annotations

from readie_evals.agent._build import (
    SESSION_RETURN,
    build_session_task,
    build_task,
    contract,
    session_rules,
)
from readie_evals.agent._json import ResponseError, extract_json
from readie_evals.agent.ports import LLM
from readie_evals.models import RawCandidate, Task

_SYSTEM = """\
You adapt real-world Python (from Kaggle notebooks and HuggingFace model cards) \
into small, self-contained functions that run on a CPU sandbox. Preserve what the \
original demonstrates -- the same libraries, the same kind of operation, and a real \
dataset -- but replace GPU use or a training loop with a small CPU equivalent, and \
swap any dataset read that needs credentials (Kaggle's own competition download, a \
private path) for a public, no-auth dataset covering the same kind of data. Follow \
the task contract exactly."""

#: A session needs at least two cells to have a real progression to adapt.
_MIN_SESSION_CELLS = 2

_SESSION_SYSTEM = """\
You adapt a real Jupyter notebook's cells (from a Kaggle kernel or a HuggingFace \
model card) into a small, self-contained multi-cell session that runs on a CPU \
sandbox. Preserve the notebook's own cell-by-cell progression, its libraries and its \
use of a real dataset -- but replace GPU use or a training loop with a small CPU \
equivalent, and swap any dataset read that needs credentials for a public, no-auth \
equivalent. Follow the task contract exactly."""


def adapt_candidate(llm: LLM, candidate: RawCandidate, *, warm: frozenset[str]) -> Task | None:
    """Rewrite one fetched candidate into a runnable task, or ``None`` to skip it."""
    user = (
        f"Category: {candidate.category}\n"
        f"Source: {candidate.source}\n\n"
        f"Adapt this snippet into one runnable task:\n\n"
        f"```python\n{candidate.raw_code}\n```\n\n"
        f"{contract(warm)}"
    )
    try:
        payload = extract_json(llm.complete(system=_SYSTEM, user=user))
    except ResponseError:
        return None
    return build_task(
        source=candidate.source,
        category=candidate.category,
        payload=payload,
        warm=warm,
        slug=candidate.slug,
        provenance=candidate.provenance,
    )


def adapt_session_candidate(
    llm: LLM,
    candidate: RawCandidate,
    *,
    warm: frozenset[str],
    source: str | None = None,
) -> Task | None:
    """Rewrite a real, multi-cell candidate into a notebook session, or ``None``.

    Needs at least two preserved cells (``candidate.raw_cells``); a candidate whose
    source collapsed to one blob (a .py script, a single code block) is skipped --
    there is no real cell-by-cell progression to adapt. ``source`` tags the result
    (default ``candidate.source``); the caller passes the distinct taxonomy source
    (e.g. ``"kaggle-notebook"``) so a session's very different timing profile does
    not land in the same report bucket as a standalone task from the same platform.
    """
    if len(candidate.raw_cells) < _MIN_SESSION_CELLS:
        return None
    numbered = "\n\n".join(
        f"# --- cell {index + 1} ---\n{cell}" for index, cell in enumerate(candidate.raw_cells)
    )
    user = (
        f"Category: {candidate.category}\n"
        f"Source: {candidate.source}\n\n"
        f"Adapt this real notebook's {len(candidate.raw_cells)} cells, in order, into "
        f"a runnable session:\n\n"
        f"```python\n{numbered}\n```\n\n"
        f"{session_rules(warm)}\n{SESSION_RETURN}"
    )
    try:
        payload = extract_json(llm.complete(system=_SESSION_SYSTEM, user=user))
    except ResponseError:
        return None
    return build_session_task(
        source=source if source is not None else candidate.source,
        category=candidate.category,
        payload=payload,
        warm=warm,
        kind="notebook",
        slug=candidate.slug,
        provenance=candidate.provenance,
    )
