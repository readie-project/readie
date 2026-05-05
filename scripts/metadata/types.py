from typing import TypedDict
from enum import StrEnum


class ResourceType(StrEnum):
    PACKAGE = "package"
    DATASET = "dataset"
    MODEL = "model"
    TOKENIZER = "tokenizer"


class MetadataEntry(TypedDict):
    base_import: str
    distribution: str
    dependencies: dict[str, str]
    size: float  # in MB
    load_time: float  # in seconds
    type: ResourceType


type Metadata = dict[str, MetadataEntry]
