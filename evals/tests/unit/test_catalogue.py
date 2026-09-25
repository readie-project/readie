from __future__ import annotations

import json

from readie_evals.catalogue import distribution_for, missing_packages, warm_imports


def _write_catalogue(path, canonical):
    path.write_text(json.dumps({"checkpoints": [{"canonical": list(canonical)}]}))
    return path


def test_warm_imports_unions_every_checkpoints_canonical(tmp_path):
    path = tmp_path / "cpu.json"
    path.write_text(
        json.dumps(
            {"checkpoints": [{"canonical": ["numpy"]}, {"canonical": ["pandas", "numpy"]}]},
        ),
    )
    assert warm_imports(path) == frozenset({"numpy", "pandas"})


def test_warm_imports_is_empty_when_the_catalogue_is_absent(tmp_path):
    assert warm_imports(tmp_path / "missing.json") == frozenset()


def test_distribution_for_maps_known_import_names():
    assert distribution_for("sklearn") == "scikit-learn"
    assert distribution_for("PIL") == "pillow"
    assert distribution_for("requests") == "requests"


def test_missing_packages_skips_warm_and_stdlib_imports():
    warm = frozenset({"numpy"})
    imports = ("numpy", "json", "sklearn", "pandas")
    # numpy is warm, json is stdlib; sklearn maps to its distribution, pandas rides through.
    assert missing_packages(imports, warm) == ("pandas", "scikit-learn")
