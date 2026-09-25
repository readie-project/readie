from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from readie_evals.fetch.kaggle import KaggleFetcher


class _NotebookApi:
    """A fake Kaggle API that pulls one kernel with a multi-cell notebook."""

    def kernels_list(self, **_kwargs):
        return [SimpleNamespace(ref="someuser/some-kernel")]

    def kernels_pull(self, _ref, path, metadata=False):  # noqa: FBT002
        notebook = {
            "cells": [
                {"cell_type": "code", "source": ["import pandas as pd\n", "print(1)\n"]},
                {"cell_type": "markdown", "source": ["# heading\n"]},
                {"cell_type": "code", "source": ["print(2)\n"]},
            ],
        }
        (Path(path) / "kernel.ipynb").write_text(json.dumps(notebook))


class _ScriptApi:
    """A fake Kaggle API that pulls one kernel with a plain .py script."""

    def kernels_list(self, **_kwargs):
        return [SimpleNamespace(ref="someuser/some-script")]

    def kernels_pull(self, _ref, path, metadata=False):  # noqa: FBT002
        (Path(path) / "kernel.py").write_text("print('hello')\n")


def test_fetch_preserves_a_notebooks_real_cells_and_slug():
    fetcher = KaggleFetcher(api=_NotebookApi())
    candidates = fetcher.fetch(category="Machine Learning", count=1)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.slug == "someuser/some-kernel"
    assert candidate.provenance == "https://www.kaggle.com/code/someuser/some-kernel"
    assert candidate.raw_cells == ("import pandas as pd\nprint(1)", "print(2)")
    assert candidate.raw_code == "import pandas as pd\nprint(1)\n\nprint(2)"


def test_fetch_treats_a_script_as_a_single_cell():
    fetcher = KaggleFetcher(api=_ScriptApi())
    candidates = fetcher.fetch(category="Machine Learning", count=1)
    assert len(candidates) == 1
    assert candidates[0].raw_cells == ("print('hello')",)
