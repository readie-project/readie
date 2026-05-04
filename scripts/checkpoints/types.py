from typing import TypedDict


class Checkpoint(TypedDict):
    imports: list[str]
    datasets: list[str]
    tokenizers: list[str]
    models: list[str]
