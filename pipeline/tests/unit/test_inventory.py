"""The environment inventory that analyze and the catalogue share."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from readie_pipeline.metadata import inventory
from readie_pipeline.metadata.inventory import Inventory, distribution_for


class _Dist:
    def __init__(self, name: str | None) -> None:
        self.metadata = {"Name": name}


def test_the_module_imports_only_the_standard_library():
    # The capture stage runs this file with the rootfs's own python, which has
    # no readie_pipeline (and need not have packaging), so any such import would
    # break it there while every test here still passed.
    tree = ast.parse(Path(inventory.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])

    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported


def test_the_distribution_is_the_first_provider_of_the_top_level_name():
    provided = {"yaml": ["PyYAML"], "google": ["protobuf", "googleapis-common-protos"]}

    assert distribution_for("yaml", provided) == "PyYAML"
    assert distribution_for("yaml.constructor", provided) == "PyYAML"
    assert distribution_for("google.protobuf", provided) == "protobuf"
    assert distribution_for("ghost", provided) == ""


def test_the_inventory_lists_the_provided_names_and_canonical_distributions(monkeypatch):
    monkeypatch.setattr(inventory, "packages_distributions", lambda: {"sklearn": ["scikit_learn"]})
    monkeypatch.setattr(
        inventory, "distributions", lambda: [_Dist("Scikit_Learn"), _Dist("PyYAML"), _Dist(None)]
    )

    found = inventory.discover_inventory()

    assert found.provided == {"sklearn": ["scikit_learn"]}
    assert found.distributions == {"scikit-learn", "pyyaml"}


def test_the_inventory_round_trips_through_json():
    original = Inventory(provided={"yaml": ["PyYAML"]}, distributions=frozenset({"pyyaml"}))

    assert Inventory.from_json(original.to_json()) == original


@pytest.mark.parametrize("text", ["{}", '{"provided": 3, "distributions": []}', "[1]"])
def test_unreadable_json_is_a_value_error(text):
    with pytest.raises(ValueError):  # noqa: PT011 - the type is the contract
        Inventory.from_json(text)


def test_run_as_a_script_it_prints_the_inventory_as_json():
    # How the capture stage uses it: the source on stdin, the result on stdout.
    out = subprocess.run(
        [sys.executable, "-"],
        input=Path(inventory.__file__).read_text(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    found = json.loads(out.strip().splitlines()[-1])
    assert "pytest" in found["distributions"]
    assert isinstance(found["provided"], dict)
