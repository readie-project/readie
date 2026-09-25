"""Turning a model response into a validated, runnable task.

Shared by generation, adaptation and repair: each asks the model for a JSON payload
carrying ``code`` (and optional ``packages``/``memory``), and this validates that
payload without executing it. Validation is static on purpose -- the client never
runs fetched code, only the sandbox does -- so it checks by parsing: the code must
compile, define the entrypoint, and not import a framework this CPU flavor cannot
serve.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from typing import Any

from readie_evals import catalogue
from readie_evals._ast import extract_imports, strip_notebook_syntax
from readie_evals.models import Task

ENTRYPOINT = "task"

#: Frameworks this CPU-only harness deliberately excludes: GPU/deep-learning stacks
#: and heavy training runtimes. A snippet importing one is discarded rather than
#: run, since no CPU checkpoint carries it and the user asked for no training.
DISALLOWED_IMPORTS = frozenset(
    {"torch", "tensorflow", "keras", "jax", "jaxlib", "flax", "cupy", "torchvision"},
)

#: The rules every generated/adapted task must follow, shared across prompts so
#: generation, adaptation and repair produce the same shape.
TASK_CONTRACT = f"""\
Produce a single self-contained Python module that defines a function named \
`{ENTRYPOINT}` taking no arguments.

Hard rules:
- Put every import INSIDE the body of `{ENTRYPOINT}` (they must run on the worker).
- CPU only. Never import torch, tensorflow, keras, jax, flax, cupy or torchvision.
- No model training beyond small scikit-learn models on tiny synthetic data.
- Use a real dataset, not synthetic data: download it INSIDE `{ENTRYPOINT}`'s own \
body (e.g. `sklearn.datasets.fetch_*`, `pandas.read_csv(<a public raw-file URL>)`, \
HuggingFace `datasets.load_dataset(...)`, `seaborn.load_dataset(...)`). The sandbox \
has real network egress, but only what `{ENTRYPOINT}` downloads itself -- nothing \
from your own environment or filesystem is available to it. Pick a small dataset (a \
few thousand rows or a capped slice of a larger one) from a source that needs no \
authentication -- never Kaggle's own competition-data download, which needs \
credentials the sandbox does not have. Fall back to a tiny synthesized input only if \
no such public dataset fits the task.
- If you use matplotlib, set a non-interactive backend \
(`import matplotlib; matplotlib.use("Agg")`) and never call `plt.show()`.
- Keep it small and fast: the download plus everything else should finish in well \
under a minute.
- `{ENTRYPOINT}` must return a small JSON-serializable summary \
(a dict, list, number or string).
- Prefer these already-available libraries: {{warm}}. If you must use another \
third-party package, list it in the "packages" field as a pip requirement.

Return ONLY a JSON object, no markdown fence, no prose:
{{{{"code": "<the full module as a string>", "packages": ["pkg", ...], \
"memory": "2Gi" or null}}}}
"""


def contract(warm: frozenset[str]) -> str:
    """The task contract, naming the checkpoint's warm libraries."""
    listed = ", ".join(sorted(warm)) if warm else "numpy, pandas, scikit-learn"
    return TASK_CONTRACT.format(warm=listed)


#: Shared hard rules for a multi-cell session, an f-string only for ``{ENTRYPOINT}``
#: -- ``{warm}`` is left for ``session_contract`` to fill in with ``.format()``.
_SESSION_RULES = f"""\
Hard rules for every cell:
- Each cell is a separate, self-contained Python module defining a function named \
`{ENTRYPOINT}` taking no arguments, exactly like a standalone task.
- Put every import INSIDE the body of `{ENTRYPOINT}` (it must run on the worker).
- CPU only. Never import torch, tensorflow, keras, jax, flax, cupy or torchvision.
- No model training beyond small scikit-learn models on tiny synthetic data.
- Use a real dataset, not synthetic data: download it inside a cell's own body \
(e.g. `sklearn.datasets.fetch_*`, `pandas.read_csv(<a public raw-file URL>)`, \
HuggingFace `datasets.load_dataset(...)`, `seaborn.load_dataset(...)`) from a source \
that needs no authentication -- never Kaggle's own competition-data download, which \
needs credentials the sandbox does not have. Keep it small (a few thousand rows or a \
capped slice) so the download stays fast.
- Cells run one after another in the same warm container, so plain Python variables \
do NOT carry over between cells (each is invoked as an independent function) -- but \
the container's local filesystem does: download the dataset once in an early cell to \
a fixed scratch path such as `/tmp/readie_session_state`, and a later cell can read \
it back from there instead of downloading it again.
- If you use matplotlib, set a non-interactive backend \
(`import matplotlib; matplotlib.use("Agg")`) and never call `plt.show()`.
- Keep each cell small and fast: the download plus everything else should finish in \
well under a minute.
- `{ENTRYPOINT}` must return a small JSON-serializable summary \
(a dict, list, number or string).
- Prefer these already-available libraries: {{warm}}. If a cell needs another \
third-party package, list it once in the top-level "packages" field.
"""

#: Public: shared with ``adapt_session_candidate``, which builds its own framing
#: paragraph instead of ``session_contract``'s "write N cells" one.
SESSION_RETURN = """
Return ONLY a JSON object, no markdown fence, no prose:
{"cells": ["<cell 1 module as a string>", "<cell 2 module as a string>", ...], \
"packages": ["pkg", ...], "memory": "2Gi" or null}
"""

_NOTEBOOK_TASK = """\
Write {cells} cells that simulate a Jupyter notebook's execution: a realistic \
multi-step {category} workflow where each cell builds on state an earlier cell left \
behind (via the scratch path described below) -- e.g. load/prepare data in cell 1, \
transform or analyze it in cell 2, summarize or model it in cell 3.
"""

_RETRY_TASK = """\
Write {cells} cells that simulate a developer iterating on the same {category} task \
inside one live session: cell 1 is a first attempt, and each following cell is a \
revised rewrite that fixes a plausible shortcoming of the previous one (a bug, an \
inefficiency, a missed edge case) -- not a resubmission of identical code. Each cell \
is a complete, independent solution to the same goal.
"""


def session_rules(warm: frozenset[str]) -> str:
    """The hard rules every session cell must follow, naming the warm libraries."""
    listed = ", ".join(sorted(warm)) if warm else "numpy, pandas, scikit-learn"
    return _SESSION_RULES.format(warm=listed)


def session_contract(warm: frozenset[str], *, kind: str, cells: int, category: str) -> str:
    """The multi-cell session contract for ``kind`` ("notebook" or "retry")."""
    task_text = (_NOTEBOOK_TASK if kind == "notebook" else _RETRY_TASK).format(
        cells=cells,
        category=category,
    )
    return f"{task_text}\n{session_rules(warm)}\n{SESSION_RETURN}"


def _defines_entrypoint(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == ENTRYPOINT
        for node in tree.body
    )


def _dedupe_packages(specs: list[str]) -> tuple[str, ...]:
    """Keep one requirement per distribution, first spelling wins."""
    seen: dict[str, str] = {}
    for spec in specs:
        text = spec.strip()
        if not text:
            continue
        base = text.replace("_", "-").split("[")[0]
        for sep in ("==", ">=", "<=", "~=", ">", "<", "!=", " "):
            base = base.split(sep)[0]
        seen.setdefault(base.lower(), text)
    return tuple(sorted(seen.values()))


def _validated_source(raw_code: object) -> tuple[str, tuple[str, ...]] | None:
    """Return runnable source and its imports, or ``None`` if it cannot run.

    Rejects code that will not parse, does not define the entrypoint, or imports a
    framework this CPU flavor excludes.
    """
    code = strip_notebook_syntax(str(raw_code)).strip()
    if not code:
        return None
    try:
        tree = ast.parse(code)
        imports = extract_imports(code)
    except SyntaxError:
        return None
    if not _defines_entrypoint(tree):
        return None
    if any(name in DISALLOWED_IMPORTS for name in imports):
        return None
    return code, imports


def validate_fix(
    payload: Any, warm: frozenset[str]
) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
    """Validate a ``{"code": ..., "packages": [...]}`` payload, or ``None`` to skip it.

    Returns (code, imports, packages). Shared by ``build_task`` and cell repair:
    both need "does this code run, and what packages does it need" without the
    rest of a full ``Task`` (repair fixes one cell in place, not a whole task).
    """
    if not isinstance(payload, dict) or "code" not in payload:
        return None
    validated = _validated_source(payload["code"])
    if validated is None:
        return None
    code, imports = validated

    declared = payload.get("packages") or []
    declared_specs = [str(item) for item in declared] if isinstance(declared, list) else []
    inferred = catalogue.missing_packages(imports, warm)
    packages = _dedupe_packages([*declared_specs, *inferred])
    return code, imports, packages


def build_task(
    *,
    source: str,
    category: str,
    payload: Any,
    warm: frozenset[str],
    slug: str = "",
    provenance: str = "",
) -> Task | None:
    """Validate a model payload into a task, or return ``None`` to skip it.

    Returns ``None`` -- rather than raising -- whenever the payload cannot become a
    runnable task, so a caller looping over many candidates can log and move on.
    """
    validated = validate_fix(payload, warm)
    if validated is None:
        return None
    code, imports, packages = validated

    memory = payload.get("memory")
    return Task(
        source=source,
        category=category,
        code=(code,),
        entrypoint=ENTRYPOINT,
        imports=imports,
        packages=packages,
        memory=None if memory is None else str(memory),
        slug=slug,
        provenance=provenance,
        created_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
    )


def _validated_cells(raw_cells: object) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Validate every cell, or return ``None`` to skip the whole session.

    A session is only as runnable as its weakest cell, so one that will not compile
    or breaks a rule drops the entire sequence rather than the one cell.
    """
    if not isinstance(raw_cells, list) or not raw_cells:
        return None
    codes: list[str] = []
    imports: dict[str, None] = {}
    for raw_cell in raw_cells:
        validated = _validated_source(raw_cell)
        if validated is None:
            return None
        code, cell_imports = validated
        codes.append(code)
        imports.update(dict.fromkeys(cell_imports))
    return tuple(codes), tuple(imports)


def build_session_task(
    *,
    source: str,
    category: str,
    payload: Any,
    warm: frozenset[str],
    kind: str,
    slug: str = "",
    provenance: str = "",
) -> Task | None:
    """Validate a model payload into a multi-cell session task, or ``None`` to skip it."""
    if not isinstance(payload, dict) or "cells" not in payload:
        return None
    validated = _validated_cells(payload["cells"])
    if validated is None:
        return None
    codes, imports = validated

    declared = payload.get("packages") or []
    declared_specs = [str(item) for item in declared] if isinstance(declared, list) else []
    inferred = catalogue.missing_packages(imports, warm)
    packages = _dedupe_packages([*declared_specs, *inferred])

    memory = payload.get("memory")
    return Task(
        source=source,
        category=category,
        code=codes,
        entrypoint=ENTRYPOINT,
        imports=imports,
        packages=packages,
        memory=None if memory is None else str(memory),
        session_kind=kind,
        slug=slug,
        provenance=provenance,
        created_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
    )
