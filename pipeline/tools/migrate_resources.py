"""Convert existing metadata identifiers without rerunning measurements.

Run once against an explicitly selected metadata file. The caller keeps the original
file for comparison; this script validates every entry before replacing the destination.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def migrate(document: dict) -> dict:
    """Preserve measured values while qualifying keys and merging dependency fields."""
    resources = {}
    for name, original in document["packages"].items():
        identifier = f"package:{name}"
        if identifier in resources:
            msg = f"duplicate identifier: {identifier}"
            raise ValueError(msg)
        facts = dict(original)
        loaded = facts.pop("loaded_modules", [])
        dependencies = facts.pop("dependencies", [])
        facts["dependencies"] = sorted(
            {value if ":" in value else f"package:{value}" for value in [*loaded, *dependencies]}
            - {identifier}
        )
        facts["resource_type"] = "package"
        for key in ["disk_size_mb", "memory_size_mb", "import_time"]:
            if facts[key] != original[key]:
                msg = f"measurement changed: {identifier}/{key}"
                raise ValueError(msg)
        resources[identifier] = facts
    converted = {
        key: value for key, value in document.items() if key not in {"packages", "resolved"}
    }
    converted["resources"] = resources
    converted["resolved"] = {
        f"package:{raw}": f"package:{resolved}"
        for raw, resolved in document.get("resolved", {}).items()
    }
    if len(resources) != len(document["packages"]):
        msg = "resource count changed"
        raise ValueError(msg)
    return converted


def main() -> None:
    """Convert a file atomically, after the schema has been approved."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    with args.source.open() as stream:
        document = json.load(stream)
    converted = migrate(document)
    temporary = args.destination.with_suffix(".json.migration")
    with temporary.open("w") as stream:
        json.dump(converted, stream, indent=2)
        stream.write("\n")
    temporary.replace(args.destination)
    print(f"Preserved {len(converted['resources'])} package measurements; identifiers converted")


if __name__ == "__main__":
    main()
