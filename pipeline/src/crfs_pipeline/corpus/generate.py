"""Synthesise request snippets with a hosted model.

An authoring step, not part of building checkpoints, which is why ``openai`` and
``python-dotenv`` are an optional extra rather than a dependency of the pipeline
image.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from crfs_pipeline.config import CorpusSettings
from crfs_pipeline.corpus.models import Corpus, CorpusError, Request
from crfs_pipeline.corpus.tree_parser import analyse

SYSTEM_PROMPT = """\
You are an expert data synthesizer acting as a diverse set of AI coding agents.

### CORE DIRECTIVE:
Simulate the code an AI agent would write for users ranging from novices to \
experts. Scripts should vary in complexity—some should be simple one-liners or \
basic scripts, while others should be comprehensive multi-step pipelines (e.g., \
combining preprocessing, feature engineering, model training, evaluation \
metrics, and visualization in a single script).

### VARIATION & NOVELTY:
- Styles: Vary between procedural scripts, object-oriented structures, and functional approaches.
- Conventions: Use different naming styles (snake_case, camelCase) and levels of abstraction.
- Anti-Repetition: Do not generate code that is identical or nearly identical to \
these previously generated topics. Even if the task is the same, you must change \
the approach, the libraries used, or the dataset/model pairing.

### ECOSYSTEM CONSTRAINTS:
1. No Documentation: No docstrings or explanatory comments.
2. Dataset Rule: If using an external dataset, include exactly one comment: \
`# DATASET USED: <kaggle_url>`. Use plausible Kaggle URLs.
3. Model Rule: Use `transformers` (Hugging Face) for state-of-the-art models. \
Use `scikit-learn` for traditional ML, including its neural network modules.
4. Complexity: Mix simple tasks with "End-to-End" workflows that include \
plotting (matplotlib/seaborn) and metrics.

### OUTPUT FORMAT:
Return ONLY a valid JSON object. No markdown formatting, no backticks, no preamble.

{
  "requests": [
    {
      "task_name": "Unique task identifier",
      "category": "Name of the category",
      "code": "Full escaped python code string"
    }
  ]
}
"""

DEFAULT_CATEGORIES: tuple[str, ...] = (
    "Exploratory Data Analysis",
    "Feature Engineering",
    "Data Preprocessing",
    "Data Science",
    "Machine Learning",
    "Natural Language Processing",
    "Model Inference",
    "Model Training",
    "Computer Vision",
    "Image Processing",
    "Time Series Analysis",
    "Recommender Systems",
    "Anomaly Detection",
    "ETL Pipelines",
    "Data Visualization",
    "Graph Processing",
)


def build_prompt(category: str, count: int, avoid: Sequence[str]) -> str:
    """The user half of the prompt, listing what not to repeat."""
    return (
        f"Generate {count} Python code snippets for the category: {category}\n"
        f"Do not generate code that is identical or nearly identical to these "
        f"previously generated topics: {', '.join(avoid)}"
    )


def parse_batch(raw: str, *, on_skip: Callable[[str, str], None] | None = None) -> list[Request]:
    """Turn one model response into requests, skipping what will not parse.

    A snippet that fails to parse is skipped and named by its *task name*. The
    previous implementation printed the entire code blob as the identifier,
    which buried the error in a screenful of Python.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"model returned invalid JSON: {exc}"
        raise CorpusError(msg) from exc

    entries = payload.get("requests") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        msg = "model response has no 'requests' list"
        raise CorpusError(msg)

    requests: list[Request] = []
    for entry in entries:
        name = str(entry.get("task_name", "<unnamed>")) if isinstance(entry, dict) else "<unnamed>"
        try:
            facts = analyse(str(entry["code"]))
            requests.append(
                Request(
                    task_name=name,
                    category=str(entry["category"]),
                    code=str(entry["code"]),
                    imports=tuple(sorted(facts.imports)),
                    datasets=tuple(sorted(facts.datasets)),
                    models=tuple(sorted(facts.models)),
                    tokenizers=tuple(sorted(facts.tokenizers)),
                )
            )
        except (SyntaxError, KeyError, TypeError, ValueError) as exc:
            if on_skip is not None:
                on_skip(name, f"{type(exc).__name__}: {exc}")

    return requests


def save(corpus: Corpus, path: Path) -> None:
    """Write the corpus atomically.

    The previous implementation rewrote the file in place after every batch, so
    an interrupted run truncated a corpus that takes hours to regenerate.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".partial")
    temp.write_text(json.dumps([r.to_json() for r in corpus], indent=2) + "\n")
    temp.replace(path)


def generate(
    settings: CorpusSettings,
    corpus_path: Path,
    *,
    client: Any = None,
    categories: Sequence[str] | None = None,
    on_line: Callable[[str], None] = print,
) -> Corpus:
    """Extend the corpus, saving after every batch.

    ``client`` is injected so this is testable without a network call and
    without importing openai at module scope.
    """
    if client is None:  # pragma: no cover - exercised only against the real API
        from openai import AzureOpenAI  # noqa: PLC0415 - optional extra, imported on use

        client = AzureOpenAI(
            api_version=settings.api_version,
            azure_endpoint=settings.endpoint,
            api_key=settings.api_key,
        )

    corpus = Corpus.load(corpus_path) if corpus_path.exists() else Corpus.of([])
    wanted = tuple(categories or settings.categories or DEFAULT_CATEGORIES)

    for category in wanted:
        for batch in range(1, settings.batches_per_category + 1):
            on_line(f"[*] {category}: batch {batch}/{settings.batches_per_category}")

            avoid = [r.task_name for r in corpus]
            response = client.chat.completions.create(
                model=settings.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": build_prompt(category, settings.requests_per_batch, avoid),
                    },
                ],
            )

            try:
                fresh = parse_batch(
                    response.choices[0].message.content or "",
                    on_skip=lambda name, why: on_line(f"    skipped {name}: {why}"),
                )
            except CorpusError as exc:
                on_line(f"    batch failed: {exc}")
                continue

            corpus = Corpus.of([*corpus.requests, *fresh])
            save(corpus, corpus_path)
            on_line(f"    +{len(fresh)} requests ({len(corpus)} total)")

    return corpus
