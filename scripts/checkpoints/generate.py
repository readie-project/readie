from dataset import Dataset
from metadata import Metadata
from .types import Checkpoint


def generate_checkpoints(dataset: Dataset, metadata: Metadata, ALPHA=0.5, S_MAX=0.5) -> list[Checkpoint]:
    checkpoints = [{
        "imports": ["pandas", "numpy"],
        "datasets": [],
        "tokenizers": [],
        "models": []
    }]
    return checkpoints
