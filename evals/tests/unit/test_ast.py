from __future__ import annotations

import pytest

from readie_evals._ast import extract_imports, strip_notebook_syntax


def test_extract_imports_returns_sorted_top_level_modules():
    code = "import numpy as np\nfrom sklearn.svm import SVC\nimport os.path\n"
    assert extract_imports(code) == ("numpy", "os", "sklearn")


def test_relative_imports_are_ignored():
    assert extract_imports("from . import sibling\nimport pandas\n") == ("pandas",)


def test_strip_notebook_syntax_drops_magics_and_shell_escapes():
    code = "%matplotlib inline\n!pip install foo\n?help\nimport pandas\n"
    assert strip_notebook_syntax(code) == "import pandas"


def test_notebook_source_parses_after_stripping():
    # A magic mid-file would break ast.parse without the strip pass.
    assert extract_imports("%time\nimport json\n") == ("json",)


def test_unparsable_source_raises():
    with pytest.raises(SyntaxError):
        extract_imports("def (:\n")
