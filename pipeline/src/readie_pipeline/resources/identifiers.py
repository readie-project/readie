"""Source-qualified resource identifiers shared by corpus and metadata."""

from __future__ import annotations

from enum import StrEnum


class ResourceType(StrEnum):
    """The source and kind of a resource."""

    PACKAGE = "package"
    KAGGLE_DATASET = "kaggle_dataset"
    KAGGLE_MODEL = "kaggle_model"
    HF_DATASET = "hf_dataset"
    HF_MODEL = "hf_model"
    HF_TOKENIZER = "hf_tokenizer"


def resource(kind: ResourceType, value: str) -> str:
    """Build a source-qualified identifier."""
    return f"{kind.value}:{value}"


def parse_resource(text: str) -> tuple[ResourceType, str]:
    """Split an identifier, rejecting unknown sources and empty names."""
    kind, separator, value = text.partition(":")
    if not separator or not value.strip():
        msg = f"resource {text!r} must have a type and a nonempty name"
        raise ValueError(msg)
    return ResourceType(kind), value
