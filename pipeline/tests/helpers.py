"""Shared builders for tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from readie_pipeline.corpus.models import Corpus, CorpusItem, Source
from readie_pipeline.metadata.models import Metadata

DATA = Path(__file__).parents[1] / "data"


def item(*imports: str, domain: str = "Data Science", source: Source = Source.AGENT_SHORT):
    """A one-step item that imports each of ``imports``."""
    return CorpusItem(
        code=("x = 1",),
        domain=domain,
        source=source,
        resources=tuple(f"package:{name}" for name in imports),
    )


_COMMON = (
    "numpy", "pandas", "matplotlib", "scipy", "sklearn", "seaborn", "torch", "transformers",
    "PIL", "cv2", "networkx", "statsmodels", "xgboost", "lightgbm", "plotly", "tensorflow",
    "datasets", "tokenizers", "nltk", "spacy", "requests", "tqdm", "yaml", "polars", "duckdb",
)  # fmt: skip


def workload_corpus(metadata: Metadata, size: int = 400, packages: int = 60) -> Corpus:
    """A corpus whose package use is Zipf-shaped over what ``metadata`` measured.

    The real corpus is generated against live services, so tests that need a
    realistic spread (a few packages almost everywhere, a long tail) build one
    from the committed measurements instead. Well-known packages come first so
    the head of the distribution is what a real workload would favour; the tail
    is filled alphabetically to keep the corpus small enough to plan quickly.
    """
    measured = {
        name for name, facts in metadata.packages.items() if facts.usable and facts.import_time > 0
    }
    names = [n for n in _COMMON if n in measured]
    names += [n for n in sorted(measured) if n not in names][: max(0, packages - len(names))]

    items: list[CorpusItem] = []
    for rank, name in enumerate(names, start=1):
        items.extend(item(name) for _ in range(max(1, size // rank)))
    return Corpus.of(items)


def write_data_dir(root: Path) -> Path:
    """A data directory with the committed metadata and a synthetic corpus."""
    (root / "datasets").mkdir(parents=True)
    (root / "metadata").mkdir()
    shutil.copy(DATA / "metadata" / "cpu.json", root / "metadata" / "cpu.json")
    corpus = workload_corpus(Metadata.load(root / "metadata" / "cpu.json"), size=150, packages=25)
    (root / "datasets" / "cpu.json").write_text(json.dumps([i.to_json() for i in corpus]))
    return root
