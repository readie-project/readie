from __future__ import annotations

from types import SimpleNamespace

from readie_evals.fetch.huggingface import HuggingFaceFetcher


class _FakeCard:
    text = (
        "intro\n\n```python\nimport transformers\nprint(1)\n```\n\n"
        "more\n\n```python\nprint(2)\n```\n"
    )


class _FakeApi:
    def list_models(self, **_kwargs):
        return [SimpleNamespace(id="org/model-name")]


def test_fetch_preserves_every_code_block_as_a_cell_and_the_slug(monkeypatch):
    from huggingface_hub import ModelCard

    monkeypatch.setattr(ModelCard, "load", classmethod(lambda _cls, *_a, **_kw: _FakeCard()))

    fetcher = HuggingFaceFetcher(api=_FakeApi())
    candidates = fetcher.fetch(category="Machine Learning", count=1)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.slug == "org/model-name"
    assert candidate.provenance == "https://huggingface.co/org/model-name"
    assert candidate.raw_cells == ("import transformers\nprint(1)", "print(2)")
    assert candidate.raw_code == "import transformers\nprint(1)"
