"""Code written by the hosted model, per flavor and domain.

Three shapes: a short one-shot script, a notebook (an ordered list of cells),
and a fix session (an agent hits an error, then repairs it over several steps).
The prompt changes with the flavor, so a ``gpu`` run asks for GPU workloads and
a ``cpu`` run asks for work that runs without one.
"""

from __future__ import annotations

import random
from collections.abc import Callable

from readie_pipeline.corpus.llm import Chat, parse_json
from readie_pipeline.corpus.models import CorpusError, Source
from readie_pipeline.corpus.popularity import Tier, pick_tier
from readie_pipeline.corpus.sources import Candidate

_SYSTEM = """\
You write realistic Python workloads of the kind data scientists and AI coding
agents run in a sandbox. Reply with a single JSON object and nothing else.

Rules for every piece of code:
- It is complete and self-contained: imports at the top, no undefined names.
- It uses only real things. Packages must exist on PyPI. Hugging Face models,
  tokenizers and datasets must be real public repositories (for example
  "bert-base-uncased", "distilbert-base-uncased-finetuned-sst-2-english",
  "imdb"). Kaggle datasets must be real (for example via
  kagglehub.dataset_download("owner/dataset")). Never write placeholders such as
  "path/to/model", "your-username/your-model" or "my_dataset".
- Load data from real hub datasets, generate it with numpy or pandas, or read a
  file the code itself writes first. Do not assume local files exist.
- Write code the way practitioners really write it: ordinary, readable, and
  typical of its kind. Do not invent exotic structure to seem original."""

_FLAVOR_GUIDANCE = {
    "cpu": (
        "Hardware: the code runs on a machine WITHOUT a GPU. Use pandas, numpy, polars, "
        "scikit-learn, xgboost, lightgbm, statsmodels, networkx, matplotlib, seaborn, "
        "plotly, opencv, Pillow, duckdb, and small Hugging Face models (up to about "
        "300M parameters) for CPU inference. Never call .cuda() or use device='cuda'."
    ),
    "gpu": (
        "Hardware: the code runs on a machine WITH a CUDA GPU and is a GPU workload. Use "
        "PyTorch, TensorFlow or JAX on the GPU, transformers, diffusers, accelerate, peft, "
        "bitsandbytes, vllm, torch_geometric, kornia, or RAPIDS (cudf, cuml, cupy). "
        "Express the domain through GPU-accelerated work: move models and tensors to "
        "cuda, train or fine-tune, or run model inference on the device."
    ),
}

_SHAPES = {
    Source.AGENT_SHORT: (
        "Each item is a short script of 5 to 40 lines doing one task. "
        '"code" is a list with exactly one string.'
    ),
    Source.AGENT_NOTEBOOK: (
        "Each item is a notebook telling one coherent story (load, explore or prepare, "
        'model or analyse, evaluate). "code" is a list of 4 to 10 strings, one per cell, '
        "in execution order. Each cell is code only, with no magics or shell commands."
    ),
    Source.AGENT_FIX: (
        "Each item is a debugging session in one persistent Python process. An agent runs "
        "a first attempt that fails with a realistic error (a wrong column name, a shape "
        "mismatch, a missing argument, a dtype problem), then fixes it step by step until "
        'the work succeeds. "code" is a list of 3 to 6 strings, one per step, each a '
        "complete cell. The first step contains the bug; each later step is the corrected "
        "or next attempt. Begin the first step with a comment naming the exception it raises."
    ),
}
_MIN_STEPS = {Source.AGENT_SHORT: 1, Source.AGENT_NOTEBOOK: 3, Source.AGENT_FIX: 2}
_MAX_STEPS = {Source.AGENT_SHORT: 1, Source.AGENT_NOTEBOOK: 12, Source.AGENT_FIX: 8}
_MAX_BATCH = {Source.AGENT_SHORT: 6, Source.AGENT_NOTEBOOK: 3, Source.AGENT_FIX: 3}

_TIER_GUIDANCE = {
    Tier.MAINSTREAM: (
        "Popularity: write the MOST widely used kind of code in this domain, like the "
        "introductory courses and the most-upvoted public notebooks. Use the most popular "
        "libraries (pandas, numpy, matplotlib, seaborn, scikit-learn, PyTorch, transformers) "
        "and famous public data and models (titanic, iris, MNIST, CIFAR-10, IMDB, "
        "bert-base-uncased, distilbert-base-uncased, resnet-50, gpt2)."
    ),
    Tier.COMMON: (
        "Popularity: write typical day-to-day professional work in this domain, using "
        "well-known libraries beyond the basics (xgboost, lightgbm, statsmodels, networkx, "
        "opencv, plotly, polars, duckdb, peft, datasets) and public datasets and models "
        "that practitioners commonly reach for."
    ),
    Tier.NICHE: (
        "Popularity: write something less common but still real: a specialised library, "
        "an unusual technique, or a lesser-known public dataset or model."
    ),
}

_ANGLES = {
    Tier.MAINSTREAM: (
        "a small business dataset",
        "a classic benchmark dataset",
        "synthetic data you generate",
        "a text corpus",
        "images",
        "a standard tutorial workflow",
    ),
    Tier.COMMON: (
        "sensor or log data",
        "financial data",
        "a text corpus",
        "images",
        "a performance-minded implementation",
        "a recent release of a popular library",
    ),
    Tier.NICHE: (
        "an uncommon but real library",
        "a specialised technique",
        "a lesser-known public dataset",
        "a recent library release",
    ),
}


class AgentSource:
    """Generates workloads with the hosted model."""

    def __init__(self, chat: Chat, rng: random.Random | None = None) -> None:
        self._chat = chat
        self._rng = rng or random.Random()  # noqa: S311 - not security sensitive

    def fetch(
        self,
        source: Source,
        domain: str,
        count: int,
        flavor: str,
        known_ref: Callable[[str | None], bool],  # noqa: ARG002 - generated code has no ref
    ) -> list[Candidate]:
        """Ask for up to ``count`` workloads and keep the well-formed ones."""
        wanted = min(count, _MAX_BATCH[source])
        tier = pick_tier(self._rng)
        angles = ", ".join(self._rng.sample(_ANGLES[tier], k=3))
        user = (
            f"Domain: {domain}.\n{_FLAVOR_GUIDANCE[flavor]}\n{_TIER_GUIDANCE[tier]}\n"
            f"{_SHAPES[source]}\n"
            f"Write {wanted} different items. Draw on: {angles}.\n"
            'Reply as {"items": [{"code": [...]}, ...]}.'
        )
        reply = parse_json(self._chat.complete(_SYSTEM, user))
        items = reply.get("items") if isinstance(reply, dict) else None
        if not isinstance(items, list):
            msg = "agent reply has no items list"
            raise CorpusError(msg)

        found: list[Candidate] = []
        for entry in items:
            code = _code(entry.get("code") if isinstance(entry, dict) else None, source)
            if code:
                found.append(Candidate(code, source, domain))
        return found

    def repair(self, candidate: Candidate, failures: list[str], flavor: str) -> Candidate | None:
        """Ask the model to replace resources that do not exist, keeping the task."""
        user = (
            f"{_FLAVOR_GUIDANCE[flavor]}\n{_SHAPES[candidate.source]}\n"
            f"The following code uses resources that do not exist: {', '.join(failures)}.\n"
            "Rewrite it to do the same task with real resources only. "
            'Reply as {"code": [...]}.\n\n' + "\n\n# --- next step ---\n".join(candidate.code)
        )
        reply = parse_json(self._chat.complete(_SYSTEM, user))
        code = _code(reply.get("code") if isinstance(reply, dict) else None, candidate.source)
        if not code:
            return None
        return Candidate(code, candidate.source, candidate.domain, ref=candidate.ref)


def _code(value: object, source: Source) -> tuple[str, ...]:
    """The code list if it has the right shape for ``source``, else empty."""
    if not isinstance(value, list) or not all(isinstance(c, str) and c.strip() for c in value):
        return ()
    if not _MIN_STEPS[source] <= len(value) <= _MAX_STEPS[source]:
        return ()
    return tuple(value)
