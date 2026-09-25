"""The real ``LLM``: Claude on Azure AI Foundry.

Uses the ``anthropic`` SDK's ``AnthropicFoundry`` client -- the Messages API served
by an Azure AI Foundry Anthropic deployment. Kept behind the ``LLM`` protocol and
built from settings, so the rest of the harness never imports ``anthropic`` and tests
never touch the network.
"""

from __future__ import annotations

from readie_evals.config import Settings

#: The Messages API requires ``max_tokens`` (the SDK has no default), so one is set
#: here rather than exposed as a knob. Generous enough for a batch of code snippets and
#: well under any current Claude model's output cap.
_MAX_TOKENS = 16000


class FoundryLLM:
    """A single-turn Claude completion over an Azure AI Foundry deployment."""

    def __init__(self, client: object, model: str) -> None:
        """Wrap an ``anthropic.AnthropicFoundry`` client for one model.

        Args:
            client: an ``AnthropicFoundry`` (typed loosely so this module does not
                depend on the SDK's types at import time).
            model: the Claude model / deployment name to call.
        """
        self._client = client
        self._model = model

    @classmethod
    def from_settings(cls, settings: Settings) -> FoundryLLM:
        """Build a client from settings, requiring the Foundry credentials."""
        settings.require_agent()
        from anthropic import AnthropicFoundry  # noqa: PLC0415 - imported on use

        client = AnthropicFoundry(api_key=settings.azure_api_key, base_url=settings.azure_endpoint)
        return cls(client, settings.azure_model)

    def complete(self, *, system: str, user: str) -> str:
        """Return the concatenated text of Claude's response."""
        response = self._client.messages.create(  # type: ignore[attr-defined]
            model=self._model,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in response.content if block.type == "text")
