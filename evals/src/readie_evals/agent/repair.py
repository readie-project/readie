"""Same-session repair of a cell that failed to run.

Called from inside the executor (see ``execution.py``), once per failing cell:
given that cell's own code and the error it raised, the model gets one attempt
at a fix. The retry runs in the *same* session as the failure -- the container
that raised is still warm, not a fresh one -- so a second failure on the same
cell is not retried again; the executor gives up and the whole task is
recorded failed.
"""

from __future__ import annotations

from readie_evals.agent._build import contract, validate_fix
from readie_evals.agent._json import ResponseError, extract_json
from readie_evals.agent.ports import LLM

_SYSTEM = """\
You fix a Python function that failed to run on a CPU sandbox. Given the code and the \
error it raised, return a corrected version that still does the same kind of work. \
If the failure is a dataset download (auth required, not found, network error), \
swap in a different small, public, no-auth dataset rather than falling back to \
synthetic data. Follow the task contract exactly."""


def repair_cell(
    llm: LLM,
    *,
    category: str,
    code: str,
    error_type: str,
    error_message: str,
    warm: frozenset[str],
) -> tuple[str, tuple[str, ...]] | None:
    """Ask the model once to fix ``code`` given its error, or return ``None``.

    Returns the fixed code and any additional packages it needs (already
    unioned with what its imports require beyond ``warm``), for the caller to
    retry in the session the failure happened in.
    """
    user = (
        f"Category: {category}\n\n"
        f"This code failed with {error_type}: {error_message}\n\n"
        f"```python\n{code}\n```\n\n"
        f"Return a corrected version.\n\n{contract(warm)}"
    )
    try:
        payload = extract_json(llm.complete(system=_SYSTEM, user=user))
    except ResponseError:
        return None
    validated = validate_fix(payload, warm)
    if validated is None:
        return None
    fixed_code, _imports, packages = validated
    return fixed_code, packages
