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
    def __init__(self, name: str | None, version: str = "1.0") -> None:
        self.metadata = {"Name": name}
        self.version = version


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
        inventory,
        "distributions",
        lambda: [_Dist("Scikit_Learn", "1.6.1"), _Dist("PyYAML", "6.0.3"), _Dist(None)],
    )

    found = inventory.discover_inventory()

    assert found.provided == {"sklearn": ["scikit_learn"]}
    assert found.distributions == {"scikit-learn", "pyyaml"}
    assert found.versions == {"scikit-learn": "1.6.1", "pyyaml": "6.0.3"}


def test_a_distribution_installed_twice_reports_the_version_an_import_finds(monkeypatch):
    # sys.path order: the first directory's copy is the one that is imported.
    monkeypatch.setattr(
        inventory, "distributions", lambda: [_Dist("numpy", "2.0.2"), _Dist("NumPy", "1.26.4")]
    )

    assert inventory.installed_versions() == {"numpy": "2.0.2"}


def test_the_inventory_round_trips_through_json():
    original = Inventory(
        provided={"yaml": ["PyYAML"]},
        distributions=frozenset({"pyyaml"}),
        versions={"pyyaml": "6.0.3"},
    )

    assert Inventory.from_json(original.to_json()) == original


def test_an_inventory_written_before_versions_were_recorded_still_loads():
    old = json.dumps({"provided": {"yaml": ["PyYAML"]}, "distributions": ["pyyaml"]})

    assert Inventory.from_json(old).versions == {}


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
