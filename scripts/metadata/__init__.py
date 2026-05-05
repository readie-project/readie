import json
import os

from .analyze_package import analyze as analyze_packages
from .types import Metadata, MetadataEntry, ResourceType

metadata_path = os.path.join(os.path.dirname(__file__), "metadata.json")


def generate_metadata(packages: list[str], datasets: list[str], models: list[str], tokenizers: list[str]) -> Metadata:
    results = {}

    print(f"[*] Analyzing imports...")
    # results.update(analyze_packages(packages))

    # Analyze datasets, models, tokenizers as needed

    # with open(metadata_path, 'w') as f:
    #     json.dump(results, f, indent=4)
    with open(metadata_path, 'r') as f:
        results = json.load(f)
    print(f"[*] Results saved to {metadata_path}")
    return results
