import sys
from types import ModuleType, SimpleNamespace

import pytest

from readie_pipeline.config import CorpusSettings
from readie_pipeline.corpus.llm import AnthropicChat, parse_json
from readie_pipeline.corpus.models import CorpusError


class FakeApiError(Exception):
    """Stands in for ``anthropic.APIError``."""


def install_fake_sdk(monkeypatch, blocks, raises=None):
    calls: dict[str, object] = {}

    class Messages:
        def create(self, **kwargs):
            calls["create"] = kwargs
            if raises:
                raise raises
            return SimpleNamespace(content=blocks)

    class Anthropic:
        def __init__(self, **kwargs):
            calls["client"] = kwargs
            self.messages = Messages()

    module = ModuleType("anthropic")
    module.Anthropic = Anthropic  # type: ignore[attr-defined]
    module.APIError = FakeApiError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return calls


def settings():
    return CorpusSettings(endpoint="https://h/anthropic", api_key="k", model="claude-haiku-4-5")


def test_the_client_targets_the_configured_endpoint_and_model(monkeypatch):
    calls = install_fake_sdk(monkeypatch, [SimpleNamespace(type="text", text="{}")])

    AnthropicChat(settings()).complete("sys", "usr")

    assert calls["client"]["base_url"] == "https://h/anthropic"
    assert calls["client"]["api_key"] == "k"
    create = calls["create"]
    assert create["model"] == "claude-haiku-4-5"
    assert create["system"] == "sys"
    assert create["messages"] == [{"role": "user", "content": "usr"}]


def test_only_text_blocks_make_up_the_reply(monkeypatch):
    install_fake_sdk(
        monkeypatch,
        [
            SimpleNamespace(type="thinking", text="ignored"),
            SimpleNamespace(type="text", text='{"a":'),
            SimpleNamespace(type="text", text=" 1}"),
        ],
    )
    assert AnthropicChat(settings()).complete("s", "u") == '{"a": 1}'


def test_a_fenced_reply_still_parses():
    assert parse_json('```json\n{"items": []}\n```') == {"items": []}


def test_a_reply_that_is_not_json_is_a_corpus_error():
    with pytest.raises(CorpusError, match="not JSON"):
        parse_json("Sure! Here you go")


def test_text_after_the_json_is_ignored():
    # Seen live: the model answered with the object and then explained it,
    # which json.loads rejects as "Extra data".
    reply = '{"flavor": "cpu", "reason": "pandas only"}\n\nThis runs on a CPU because...'
    assert parse_json(reply) == {"flavor": "cpu", "reason": "pandas only"}


def test_text_before_the_json_is_ignored():
    assert parse_json('Here is the result:\n{"items": [1, 2]}') == {"items": [1, 2]}


def test_a_braced_string_inside_the_json_does_not_end_it_early():
    assert parse_json('{"code": ["d = {1: 2}"]} trailing') == {"code": ["d = {1: 2}"]}


def test_a_failed_model_call_is_a_corpus_error_not_a_crash(monkeypatch):
    # An exhausted retry budget must cost one attempt, not end a run of thousands.
    install_fake_sdk(monkeypatch, [], raises=FakeApiError("429 rate limited"))
    with pytest.raises(CorpusError, match="model call failed: 429"):
        AnthropicChat(settings()).complete("s", "u")


def test_the_client_retries_more_than_the_default_and_allows_long_replies(monkeypatch):
    calls = install_fake_sdk(monkeypatch, [SimpleNamespace(type="text", text="{}")])
    AnthropicChat(settings()).complete("s", "u")
    assert calls["client"]["max_retries"] >= 5
    assert calls["create"]["max_tokens"] >= 16000
