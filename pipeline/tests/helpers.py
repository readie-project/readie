"""Shared builders for tests."""

from __future__ import annotations

import json
from pathlib import Path

from readie_pipeline.corpus.models import Corpus, CorpusItem, Source
from readie_pipeline.metadata.models import Metadata, ResourceFacts

DATA = Path(__file__).parents[1] / "data"


def item(*imports: str, domain: str = "Data Science", source: Source = Source.AGENT_SHORT):
    """A one-step item that imports each of ``imports``."""
    return CorpusItem(
        code=("x = 1",),
        domain=domain,
        source=source,
        resources=tuple(name if ":" in name else f"package:{name}" for name in imports),
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
        name for name, facts in metadata.resources.items() if facts.usable and facts.import_time > 0
    }
    names = [f"package:{n}" for n in _COMMON if f"package:{n}" in measured]
    names += [n for n in sorted(measured) if n not in names][: max(0, packages - len(names))]

    items: list[CorpusItem] = []
    for rank, name in enumerate(names, start=1):
        items.extend(item(name) for _ in range(max(1, size // rank)))
    return Corpus.of(items)


def write_data_dir(root: Path) -> Path:
    """A data directory with the committed metadata and a synthetic corpus."""
    (root / "datasets").mkdir(parents=True)
    (root / "metadata").mkdir()
    metadata = Metadata(
        {
            f"package:{name}": ResourceFacts(
                base_import=name,
                distribution=name,
                disk_size_mb=10,
                memory_size_mb=10,
                import_time=0.1 + index / 10,
            )
            for index, name in enumerate(_COMMON)
        },
        distributions={"pandas": "2.2.0"},
    )
    (root / "metadata" / "cpu.json").write_text(json.dumps(metadata.to_json()))
    corpus = workload_corpus(metadata, size=150, packages=25)
    (root / "datasets" / "cpu.json").write_text(json.dumps([i.to_json() for i in corpus]))
    return root
