"""The domains a workload can belong to, and what to search for in each."""

from __future__ import annotations

DOMAINS: tuple[str, ...] = (
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

#: Search phrases per domain for the services that fetch real code. A domain
#: is a loose label, so each one is several concrete queries; a source picks
#: among them at random to vary what it finds.
SEARCH_TERMS: dict[str, tuple[str, ...]] = {
    "Exploratory Data Analysis": ("exploratory data analysis", "eda", "data profiling"),
    "Feature Engineering": ("feature engineering", "feature selection", "target encoding"),
    "Data Preprocessing": ("data cleaning", "preprocessing", "missing values imputation"),
    "Data Science": ("data science", "statistical analysis", "hypothesis testing"),
    "Machine Learning": ("classification", "regression", "gradient boosting", "random forest"),
    "Natural Language Processing": ("nlp", "text classification", "sentiment analysis", "ner"),
    "Model Inference": ("inference", "batch prediction", "text generation", "pipeline"),
    "Model Training": ("fine-tuning", "training loop", "transfer learning", "hyperparameter"),
    "Computer Vision": ("image classification", "object detection", "segmentation"),
    "Image Processing": ("image processing", "opencv", "image augmentation"),
    "Time Series Analysis": ("time series", "forecasting", "arima", "lstm forecasting"),
    "Recommender Systems": (
        "recommender system",
        "collaborative filtering",
        "matrix factorization",
    ),
    "Anomaly Detection": ("anomaly detection", "outlier detection", "fraud detection"),
    "ETL Pipelines": ("etl", "data pipeline", "data wrangling", "parquet"),
    "Data Visualization": ("data visualization", "plotly", "seaborn", "matplotlib"),
    "Graph Processing": ("graph", "networkx", "graph neural network", "node classification"),
}

#: Extra search phrases for the Hugging Face Hub. Hub search matches repository
#: names only, so a phrase like "feature engineering" finds one repository, while
#: the library and task names people actually put in names ("sklearn", "tabular",
#: "forecasting") find many. Each domain's list is added to ``SEARCH_TERMS``.
HUB_TERMS: dict[str, tuple[str, ...]] = {
    "Exploratory Data Analysis": ("tabular", "pandas", "dataset analysis"),
    "Feature Engineering": ("tabular", "sklearn", "xgboost", "skops"),
    "Data Preprocessing": ("preprocessing", "tokenizer", "tabular", "sklearn"),
    "Data Science": ("sklearn", "tabular", "regression", "skops"),
    "Machine Learning": ("sklearn", "xgboost", "random-forest", "skops"),
    "Natural Language Processing": ("bert", "text-generation", "summarization"),
    "Model Inference": ("inference", "gradio", "pipeline"),
    "Model Training": ("fine-tuned", "finetune", "trainer", "lora"),
    "Computer Vision": ("vit", "resnet", "yolo"),
    "Image Processing": ("opencv", "image", "diffusion"),
    "Time Series Analysis": ("time-series", "forecasting", "lstm"),
    "Recommender Systems": ("recommendation", "recommender", "ncf"),
    "Anomaly Detection": ("anomaly", "outlier", "fraud"),
    "ETL Pipelines": ("etl", "parquet", "csv", "dataframe"),
    "Data Visualization": ("visualization", "plot", "gradio"),
    "Graph Processing": ("graph", "gnn", "networkx"),
}
