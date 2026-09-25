"""The domains and sources an evaluation ranges over.

The categories mirror the pipeline's corpus taxonomy so an eval reads against the
same domains the checkpoints were planned for. The sources are the three the
harness fetches from. Each category also carries a few search keywords, used to
steer the Kaggle and HuggingFace fetchers toward code in that domain.
"""

from __future__ import annotations

#: The workload domains, matching ``readie_pipeline``'s ``DEFAULT_CATEGORIES``.
#: "Deep Learning" is not separate: on CPU it is inference, which lands under
#: Natural Language Processing, Computer Vision or Model Inference.
CATEGORIES: tuple[str, ...] = (
    "Exploratory Data Analysis",
    "Feature Engineering",
    "Data Preprocessing",
    "Data Science",
    "Machine Learning",
    "Natural Language Processing",
    "Model Inference",
    "Computer Vision",
    "Image Processing",
    "Time Series Analysis",
    "Recommender Systems",
    "Anomaly Detection",
    "ETL Pipelines",
    "Data Visualization",
    "Graph Processing",
)

#: Where a workload's code comes from. ``generated`` is the model writing fresh code;
#: the other two are freshly fetched from the named platform. The default set
#: ``fetch`` fills; ``SESSION_SOURCES`` are opt-in (pass them to ``--sources``
#: explicitly) since each session task costs several times an ordinary one.
SOURCES: tuple[str, ...] = ("kaggle", "huggingface", "generated")

#: Multi-cell, fully self-generated sessions: ``generated-notebook`` cells build on
#: state an earlier cell left behind; ``generated-retry`` cells are independent
#: attempts at the same goal, simulating a developer iterating inside one live
#: session. See ``readie_evals.models.SESSION_KINDS``.
GENERATED_SESSION_SOURCES: tuple[str, ...] = ("generated-notebook", "generated-retry")

#: Multi-cell sessions built from a real Kaggle kernel's or HuggingFace card's own
#: cell sequence -- adapted, not synthesized, so the same trust tier as plain
#: ``kaggle``/``huggingface`` (not ``GENERATED_SOURCES``). Only ``"notebook"``: a
#: real notebook already has a natural cell-by-cell progression; "retry" describes a
#: developer iterating on their own code, which only makes sense for generated code.
REAL_SESSION_SOURCES: tuple[str, ...] = ("kaggle-notebook", "huggingface-notebook")

SESSION_SOURCES: tuple[str, ...] = GENERATED_SESSION_SOURCES + REAL_SESSION_SOURCES

#: Every source ``--sources`` accepts, including the opt-in session ones.
ALL_SOURCES: tuple[str, ...] = SOURCES + SESSION_SOURCES

#: Sources whose code the harness wrote itself, trusted enough for the ``local``
#: target to run in-process with no sandbox.
GENERATED_SOURCES: frozenset[str] = frozenset({"generated", *GENERATED_SESSION_SOURCES})

#: Free-text search terms per category, used by the fetchers to find relevant
#: real code. Kept deliberately broad; a fetcher may use one or several.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Exploratory Data Analysis": ("eda", "data exploration", "pandas profiling"),
    "Feature Engineering": ("feature engineering", "feature extraction", "encoding"),
    "Data Preprocessing": ("data cleaning", "preprocessing", "normalization"),
    "Data Science": ("data science", "statistics", "hypothesis testing"),
    "Machine Learning": ("scikit-learn", "classification", "regression"),
    "Natural Language Processing": ("nlp", "text classification", "tokenization"),
    "Model Inference": ("inference", "prediction", "transformers pipeline"),
    "Computer Vision": ("image classification", "computer vision", "object detection"),
    "Image Processing": ("image processing", "pillow", "filters"),
    "Time Series Analysis": ("time series", "forecasting", "seasonality"),
    "Recommender Systems": ("recommender", "collaborative filtering", "matrix factorization"),
    "Anomaly Detection": ("anomaly detection", "outlier detection", "isolation forest"),
    "ETL Pipelines": ("etl", "data pipeline", "transformation"),
    "Data Visualization": ("visualization", "matplotlib", "seaborn"),
    "Graph Processing": ("graph", "networkx", "centrality"),
}


def keywords_for(category: str) -> tuple[str, ...]:
    """Return the search keywords for a category, or the category name itself."""
    return CATEGORY_KEYWORDS.get(category, (category,))
