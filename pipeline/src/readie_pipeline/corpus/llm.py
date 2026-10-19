"""The hosted model, behind one small seam.

Everything that talks to the model goes through :class:`Chat`, so tests inject
a fake and nothing imports ``anthropic`` at module scope.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from readie_pipeline.config import CorpusSettings
from readie_pipeline.corpus.models import CorpusError


class Chat(Protocol):
    """One system prompt, one user prompt, one JSON reply."""

    def complete(self, system: str, user: str) -> str:
        """Return the model's reply text."""
        ...


class AnthropicChat:
    """:class:`Chat` over the Anthropic Messages API at the configured endpoint.

    The endpoint is the Azure-hosted Anthropic route (``.../anthropic``); the
    SDK appends ``/v1/messages`` to it. The API has no JSON-object mode, so the
    system prompt asks for JSON and :func:`parse_json` reads the reply.
    """

    def __init__(self, settings: CorpusSettings, max_tokens: int = 16384) -> None:
        from anthropic import Anthropic, APIError  # noqa: PLC0415 - optional extra

        self._model = settings.model
        self._max_tokens = max_tokens
        self._api_error = APIError
        # The SDK backs off and retries rate limits and server errors; a run of
        # thousands of calls will meet some, so allow more retries than the default.
        self._client = Anthropic(
            base_url=settings.endpoint, api_key=settings.api_key, max_retries=6
        )

    def complete(self, system: str, user: str) -> str:
        """Call the Messages API and return the reply text.

        Raises:
            CorpusError: The call failed after the SDK's retries. It is reported
                as a corpus error so the generator treats it as one failed
                attempt and carries on, as it does for a malformed reply, and a
                long run is not ended by a single bad call.
        """
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except self._api_error as exc:
            msg = f"model call failed: {exc}"
            raise CorpusError(msg) from exc
        return "".join(block.text for block in response.content if block.type == "text")


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(text: str) -> Any:
    """Parse the first JSON value in a model reply.

    Models often wrap the JSON in a Markdown fence or follow it with a sentence
    of explanation, so this decodes one value from the first ``{`` or ``[`` and
    ignores whatever comes after it.

    Raises:
        CorpusError: The reply holds no JSON value.
    """
    cleaned = _FENCE.sub("", text.strip())
    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
    try:
        value, _ = json.JSONDecoder().raw_decode(cleaned[min(starts) :] if starts else cleaned)
    except json.JSONDecodeError as exc:
        msg = f"model reply is not JSON: {exc}"
        raise CorpusError(msg) from exc
    return value
