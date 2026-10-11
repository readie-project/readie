import json

from readie_pipeline.corpus.sources.notebooks import notebook_cells, python_blocks


def test_only_non_empty_code_cells_are_kept_in_order():
    nb = {
        "cells": [
            {"cell_type": "markdown", "source": ["# title"]},
            {"cell_type": "code", "source": ["import pandas as pd\n", "df = pd.DataFrame()"]},
            {"cell_type": "code", "source": ["%matplotlib inline"]},
            {"cell_type": "code", "source": "x = 1"},
        ]
    }
    assert notebook_cells(json.dumps(nb)) == ("import pandas as pd\ndf = pd.DataFrame()", "x = 1")


def test_text_that_is_not_a_notebook_yields_nothing():
    assert notebook_cells("not json") == ()
    assert notebook_cells('{"nope": 1}') == ()


def test_fenced_python_blocks_are_pulled_from_markdown():
    text = "intro\n```python\nimport x\ny = 1\n```\n```bash\npip install x\n```\n```py\nz = 2\n```"
    assert python_blocks(text) == ["import x\ny = 1", "z = 2"]
