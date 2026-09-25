"""The seams the agent and fetchers are written against.

Protocols, not base classes: a consumer declares the shape it needs and any
implementation satisfies it structurally, so a test can pass a fake without
subclassing and without importing the OpenAI or platform SDKs.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from readie_evals.models import RawCandidate


@runtime_checkable
class LLM(Protocol):
    """A single-turn text completion, given a system and a user prompt."""

    def complete(self, *, system: str, user: str) -> str:
        """Return the model's text response to ``user`` under ``system``."""
        ...


@runtime_checkable
class Fetcher(Protocol):
    """Sources raw code candidates for a domain."""

    @property
    def source(self) -> str:
        """The source name these candidates are tagged with."""
        ...

    def fetch(self, *, category: str, count: int) -> list[RawCandidate]:
        """Return up to ``count`` candidates for ``category``."""
        ...
