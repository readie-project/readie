import pytest

from readie_pipeline.corpus.flavor import FlavorJudge
from readie_pipeline.corpus.models import CorpusError


class Chat:
    def __init__(self, reply):
        self.reply = reply
        self.seen: list[tuple[str, str]] = []

    def complete(self, system, user):
        self.seen.append((system, user))
        return self.reply


def test_the_models_classification_is_returned():
    assert FlavorJudge(Chat('{"flavor": "gpu", "reason": "uses cuda"}')).judge(["x"]) == "gpu"


def test_every_step_is_shown_to_the_model():
    chat = Chat('{"flavor": "cpu", "reason": "r"}')
    FlavorJudge(chat).judge(["step_one()", "step_two()"])
    assert "step_one()" in chat.seen[0][1]
    assert "step_two()" in chat.seen[0][1]


@pytest.mark.parametrize("reply", ['{"flavor": "tpu"}', "[]", "not json"])
def test_an_unusable_classification_is_an_error(reply):
    with pytest.raises(CorpusError):
        FlavorJudge(Chat(reply)).judge(["x"])


class Sequenced:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        return self.replies.pop(0)


def test_a_malformed_reply_is_retried_once():
    # Seen live: one reply in 150 was not JSON and cost a real item.
    chat = Sequenced("{flavor: cpu}", '{"flavor": "cpu", "reason": "r"}')
    assert FlavorJudge(chat).judge(["x"]) == "cpu"
    assert chat.calls == 2


def test_two_malformed_replies_are_an_error():
    chat = Sequenced("nope", "still nope")
    with pytest.raises(CorpusError):
        FlavorJudge(chat).judge(["x"])
    assert chat.calls == 2


def test_a_good_first_reply_is_not_asked_twice():
    chat = Sequenced('{"flavor": "gpu", "reason": "r"}')
    assert FlavorJudge(chat).judge(["x"]) == "gpu"
    assert chat.calls == 1
