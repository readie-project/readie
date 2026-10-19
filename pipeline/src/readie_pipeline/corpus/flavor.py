"""Decide which flavor a piece of code belongs to.

A run generates one flavor. Code fetched from Kaggle or Hugging Face was not
written for either, so the model judges it: code that needs a GPU (or exists
to exercise one) is a ``gpu`` workload, and code that runs acceptably on a CPU
is a ``cpu`` workload. Only items matching the run's flavor are kept.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from readie_pipeline.corpus.llm import Chat, parse_json
from readie_pipeline.corpus.models import CorpusError

FLAVORS = ("cpu", "gpu")
_MAX_CHARS = 12_000

_SYSTEM = """\
You classify Python workloads by the hardware they need. Reply with a JSON object
{"flavor": "cpu" | "gpu", "reason": "<one sentence>"}.

- "gpu": the code needs a CUDA GPU to run as intended, or is a GPU workload:
  it moves models or tensors to cuda, trains or fine-tunes a deep network,
  runs a large model for inference, or uses GPU-only libraries (cupy, cudf,
  vllm, bitsandbytes, flash-attn, nvidia-*).
- "cpu": it runs acceptably on a CPU: dataframes, classical machine learning,
  plotting, ETL, small-model inference, graph algorithms, image processing
  with CPU libraries.
When the code works on either, choose "cpu" unless it is clearly built around a GPU."""


class Classifier(Protocol):
    """Names the flavor a piece of code belongs to."""

    def judge(self, code: Sequence[str]) -> str:
        """Return ``"cpu"`` or ``"gpu"``."""
        ...


class FlavorJudge:
    """Classifies code as ``cpu`` or ``gpu`` with the hosted model."""

    def __init__(self, chat: Chat) -> None:
        self._chat = chat

    def judge(self, code: Sequence[str]) -> str:
        """Return ``"cpu"`` or ``"gpu"``, asking again once if the reply is unusable.

        Raises:
            CorpusError: Neither reply is a valid classification.
        """
        try:
            return self._ask(code)
        except CorpusError:
            # Seen live: one reply in a long run was not JSON. A second ask
            # almost always answers, and losing a real item to a formatting slip
            # is worse than one more call.
            return self._ask(code)

    def _ask(self, code: Sequence[str]) -> str:
        body = "\n\n# --- next step ---\n".join(code)[:_MAX_CHARS]
        reply = parse_json(self._chat.complete(_SYSTEM, body))
        flavor = reply.get("flavor") if isinstance(reply, dict) else None
        if flavor not in FLAVORS:
            msg = f"flavor judge returned {reply!r}"
            raise CorpusError(msg)
        return str(flavor)
