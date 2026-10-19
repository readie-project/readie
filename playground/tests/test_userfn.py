import ast
import os
import sys
from collections.abc import Iterator
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


# ---------------------------------------------------------------------------
# Steps (the evaluation path)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_namespaces() -> Iterator[None]:
    """Session namespaces live on a sys.modules entry; each test starts without one."""
    sys.modules.pop("__readie_eval__", None)
    yield
    sys.modules.pop("__readie_eval__", None)


def run_step(source: str, key: str, index: int, tmp_path: Path) -> None:
    with userfn.loaded_step(source, key, index, tmp_path) as main:
        main()


def test_import_statements_are_found_deduplicated_and_made_legal_inside_a_function() -> None:
    source = (
        "from __future__ import annotations\n"
        "import numpy as np\nimport numpy as np\n"
        "from sklearn.svm import LinearSVC\n"
        "from os.path import *\n"
        "from . import sibling\n"
        "def f():\n    import json\n"
    )
    assert userfn.import_statements(source) == [
        "import numpy as np",
        "from sklearn.svm import LinearSVC",
        "import os.path",
        "import json",
    ]


def test_code_that_does_not_parse_has_no_hoisted_imports() -> None:
    assert userfn.import_statements("import numpy\ndef broken(:") == []


def test_the_client_sees_the_steps_imports_just_as_it_would_for_a_users_own_function(
    tmp_path: Path,
) -> None:
    # The router picks a checkpoint from these strings, so they must match exactly.
    source = "import pandas as pd\nfrom sklearn.svm import LinearSVC\nfrom os.path import *\n"
    with userfn.loaded_step(source, "k", 0, tmp_path) as main:
        found = set(extract_imports(main))
    assert {"pandas", "sklearn.svm.LinearSVC", "os.path"} <= found


def test_a_conditional_import_cannot_break_the_step_before_the_code_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = "try:\n    import no_such_package_xyz\nexcept ImportError:\n    print('fell back')\n"
    run_step(source, "k", 0, tmp_path)
    assert "fell back" in capsys.readouterr().out


def test_a_later_step_of_the_same_session_sees_what_an_earlier_one_defined(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_step("x = 41\ndef bump(v):\n    return v + 1", "session-a", 0, tmp_path)
    run_step("print(bump(x))", "session-a", 1, tmp_path)
    assert "42" in capsys.readouterr().out


def test_another_session_has_its_own_names(tmp_path: Path) -> None:
    run_step("x = 1", "session-a", 0, tmp_path)
    with pytest.raises(NameError):
        run_step("print(x)", "session-b", 0, tmp_path)


def test_code_runs_with_module_semantics_not_as_a_function_body(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = (
        "from math import *\n"
        "class Shape:\n    sides = 4\n    double = sides * 2\n"
        "print(__name__, Shape.double, floor(2.5))\n"
    )
    run_step(source, "k", 0, tmp_path)
    assert "__main__ 8 2" in capsys.readouterr().out


def test_an_exception_in_a_step_propagates_so_readie_reports_it(tmp_path: Path) -> None:
    with pytest.raises(ZeroDivisionError):
        run_step("1 / 0", "k", 0, tmp_path)


def test_plots_do_not_need_a_display(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MPLBACKEND", raising=False)
    run_step("pass", "k", 0, tmp_path)
    assert os.environ["MPLBACKEND"] == "Agg"


def test_a_step_module_is_cleaned_up_after_use(tmp_path: Path) -> None:
    before = set(sys.modules)
    run_step("x = 1", "k", 0, tmp_path)
    assert not list(tmp_path.glob("pg_*.py"))
    assert not {m for m in set(sys.modules) - before if m.startswith("pg_")}


@pytest.mark.parametrize("source", ["x = (1 +", "def f(:", "print('a'"])
def test_check_step_reports_a_syntax_error_without_running_anything(source: str) -> None:
    message = userfn.check_step(source)
    assert message is not None
    assert message.startswith("SyntaxError:")


def test_check_step_accepts_valid_code() -> None:
    assert userfn.check_step("import os\nprint(os.getcwd())") is None


def test_the_step_source_is_embedded_literally_so_awkward_code_survives(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = 'text = """triple \'quoted\' with "double" and \\\\ backslash\n"""\nprint(len(text))\n'
    run_step(source, "k", 0, tmp_path)
    assert capsys.readouterr().out.strip().isdigit()
