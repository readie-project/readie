"""The values that flow through the harness.

``RawCandidate`` is a snippet as fetched or generated, before adaptation.
``Task`` is a runnable workload: a module defining an entrypoint function, tagged
with its source and domain and carrying the Readie config it should run under.

Frozen dataclasses with explicit ``from_json``/``to_json`` rather than TypedDicts,
matching the pipeline's corpus models: a load-time error beats a silent ``KeyError``
in whatever reads the corpus next.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


class TaskError(Exception):
    """A task on disk is not what this code expects."""


#: How a multi-cell task's sequence was produced: ``"notebook"`` cells build on
#: state an earlier cell left behind; ``"retry"`` cells are independent attempts at
#: the same goal, simulating a developer iterating inside one live session.
SESSION_KINDS: tuple[str, ...] = ("notebook", "retry")


@dataclass(frozen=True, slots=True)
class RawCandidate:
    """A snippet as sourced, before the agent adapts it into a runnable task."""

    source: str
    category: str
    raw_code: str
    raw_cells: tuple[str, ...] = field(default_factory=tuple)
    """The individual code cells this candidate came from, when the source
    preserves cell boundaries (a Kaggle notebook's cells, a HuggingFace card's code
    blocks) -- empty when there's only ever one blob (a .py script, a single
    block). Used to adapt a real, multi-cell notebook into a session task."""
    slug: str = ""
    """The platform's own identifier: a Kaggle kernel ref ("user/kernel-slug") or a
    HuggingFace repo id ("org/model-name"), or "" (generated)."""
    provenance: str = ""
    """Where it came from: the Kaggle kernel or HuggingFace model URL, or ""."""


def make_task_id(source: str, code: Sequence[str]) -> str:
    """A stable short id for a task, from its source and code.

    Deterministic so re-fetching an identical snippet dedupes against the corpus,
    and so results key to the same task across runs.
    """
    digest = hashlib.sha256("\0".join((source, *code)).encode()).hexdigest()
    return digest[:16]


@dataclass(frozen=True, slots=True)
class Task:
    """One runnable workload."""

    source: str
    category: str
    code: tuple[str, ...]
    """One or more modules, each defining ``entrypoint`` as a no-argument function
    with its imports inside the body (they run on the worker, not the client). A
    single entry runs as one standalone call; more than one runs as a session --
    the cells execute in order against one warm container, and ``session_kind``
    says how they were produced."""
    entrypoint: str = "task"
    imports: tuple[str, ...] = field(default_factory=tuple)
    packages: tuple[str, ...] = field(default_factory=tuple)
    """PyPI requirements to install before the call, for imports the checkpoint
    does not already carry."""
    memory: str | None = None
    timeout: float | None = None
    session_kind: str | None = None
    """``None`` for a standalone task, else one of ``SESSION_KINDS``."""
    slug: str = ""
    """The platform's own identifier ("user/kernel-slug", "org/model-name"), or ""
    (generated)."""
    provenance: str = ""
    """The Kaggle kernel or HuggingFace model URL this task came from, or ""."""
    created_at: str = ""

    @property
    def is_session(self) -> bool:
        """Whether this task's cells run as a session (more than one code entry)."""
        return len(self.code) > 1

    @property
    def id(self) -> str:
        """The stable id derived from source and code."""
        return make_task_id(self.source, self.code)

    @classmethod
    def from_json(cls, raw: Mapping[str, Any], *, index: int) -> Task:
        """Build a task, naming the offending entry when it cannot."""
        missing = {"source", "category", "code"} - raw.keys()
        if missing:
            msg = f"task entry {index} is missing {', '.join(sorted(missing))}"
            raise TaskError(msg)
        timeout = raw.get("timeout")
        session_kind = raw.get("session_kind")
        if session_kind is not None and session_kind not in SESSION_KINDS:
            msg = f"task entry {index} has an unknown session_kind {session_kind!r}"
            raise TaskError(msg)
        return cls(
            source=str(raw["source"]),
            category=str(raw["category"]),
            code=_code(raw["code"], index=index),
            entrypoint=str(raw.get("entrypoint", "task")),
            imports=_strings(raw.get("imports")),
            packages=_strings(raw.get("packages")),
            memory=None if raw.get("memory") is None else str(raw["memory"]),
            timeout=None if timeout is None else float(timeout),
            session_kind=None if session_kind is None else str(session_kind),
            slug=str(raw.get("slug", "")),
            provenance=str(raw.get("provenance", "")),
            created_at=str(raw.get("created_at", "")),
        )

    def to_json(self) -> dict[str, Any]:
        """Render to the on-disk shape, id included for readability."""
        return {
            "id": self.id,
            "source": self.source,
            "category": self.category,
            "code": list(self.code),
            "entrypoint": self.entrypoint,
            "imports": list(self.imports),
            "packages": list(self.packages),
            "memory": self.memory,
            "timeout": self.timeout,
            "session_kind": self.session_kind,
            "slug": self.slug,
            "provenance": self.provenance,
            "created_at": self.created_at,
        }


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        msg = f"expected a list of strings, got {type(value).__name__}"
        raise TaskError(msg)
    return tuple(str(item) for item in value)


def _code(value: object, *, index: int) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence) or not value:
        msg = f"task entry {index}'s 'code' must be a non-empty list of strings"
        raise TaskError(msg)
    return tuple(str(item) for item in value)
