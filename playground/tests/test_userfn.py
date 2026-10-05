import ast
import sys
from pathlib import Path

import pytest
from readie.resources import extract_imports

from readie_playground import userfn

ADVERSARIAL = [
    "print(1)",
    '"""\nimport os; os.system("x")\n"""',
    "x = 1 \\\n+ 2",
    "import os; os.system('id')",
    "def f():\n    pass\n\nf()",
]


@pytest.mark.parametrize("code", ADVERSARIAL)
def test_code_never_escapes_main(code: str) -> None:
    tree = ast.parse(userfn.build_module(code))
    # One import and one function: nothing of the visitor's runs at import time.
    assert [type(n) for n in tree.body] == [ast.ImportFrom, ast.FunctionDef]


def test_check_reports_the_visitors_own_line() -> None:
    assert userfn.check("print(1)") is None
    error = userfn.check("x = 1\ny = = 2")
    assert error is not None
    assert "line 2" in error


def test_loaded_exposes_source_so_imports_are_extracted(tmp_path: Path) -> None:
    with userfn.loaded("import math\nprint(math.pi)", tmp_path) as main:
        assert "math" in extract_imports(main)


def test_loaded_cleans_up_after_success_and_failure(tmp_path: Path) -> None:
    with userfn.loaded("print(1)", tmp_path):
        assert list(tmp_path.glob("pg_*.py"))
    assert not list(tmp_path.glob("pg_*.py"))
    assert not [m for m in sys.modules if m.startswith("pg_")]

    with pytest.raises(RuntimeError), userfn.loaded("print(1)", tmp_path):
        raise RuntimeError
    assert not list(tmp_path.glob("pg_*.py"))
    assert not [m for m in sys.modules if m.startswith("pg_")]


def test_importing_does_not_run_the_visitors_code(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    with userfn.loaded(f"open({str(marker)!r}, 'w').close()", tmp_path):
        assert not marker.exists()
