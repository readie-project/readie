"""A fake LLM that returns scripted responses, so tests never hit the network."""

from __future__ import annotations


class FakeLLM:
    """Returns queued responses in order; records the prompts it was given."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self._responses.pop(0) if self._responses else "{}"
