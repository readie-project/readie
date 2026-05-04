from typing import TypedDict


class DatasetEntry(TypedDict):
    task_name: str
    code_snippet: str
    category: str
    imports: list[str]
    datasets: list[str]
    models: list[str]
    tokenizers: list[str]


type Dataset = list[DatasetEntry]
