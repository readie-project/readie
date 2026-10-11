import json
import random

from readie_pipeline.corpus.models import Source
from readie_pipeline.corpus.sources import Candidate
from readie_pipeline.corpus.sources.agent import AgentSource


class Chat:
    def __init__(self, reply):
        self.reply = reply
        self.users: list[str] = []

    def complete(self, system, user):
        self.users.append(user)
        return json.dumps(self.reply)


def never(_ref):
    return False


def fetch(reply, source=Source.AGENT_SHORT, flavor="cpu", count=2):
    chat = Chat(reply)
    found = AgentSource(chat, random.Random(0)).fetch(source, "Data Science", count, flavor, never)
    return found, chat


def test_the_prompt_asks_for_the_requested_flavor_and_domain():
    _, cpu = fetch({"items": []}, flavor="cpu")
    _, gpu = fetch({"items": []}, flavor="gpu")
    assert "WITHOUT a GPU" in cpu.users[0]
    assert "WITH a CUDA GPU" in gpu.users[0]
    assert "Data Science" in cpu.users[0]


def test_each_source_asks_for_its_own_shape():
    _, notebook = fetch({"items": []}, Source.AGENT_NOTEBOOK)
    _, fix = fetch({"items": []}, Source.AGENT_FIX)
    assert "notebook" in notebook.users[0]
    assert "debugging session" in fix.users[0]


def test_well_formed_items_become_candidates_with_no_ref():
    found, _ = fetch({"items": [{"code": ["x = 1"]}, {"code": ["y = 2"]}]})
    assert [c.code for c in found] == [("x = 1",), ("y = 2",)]
    assert all(c.ref is None and c.source is Source.AGENT_SHORT for c in found)


def test_items_with_the_wrong_number_of_steps_are_dropped():
    found, _ = fetch({"items": [{"code": ["a", "b"]}, {"code": []}, {"code": ["ok = 1"]}]})
    assert [c.code for c in found] == [("ok = 1",)]


def test_a_fix_session_needs_more_than_one_step():
    found, _ = fetch({"items": [{"code": ["only"]}, {"code": ["bad", "good"]}]}, Source.AGENT_FIX)
    assert [c.code for c in found] == [("bad", "good")]


def test_a_repair_reports_the_resources_that_failed_and_keeps_the_task():
    chat = Chat({"code": ["import pandas"]})
    candidate = Candidate(("m = AutoModel.from_pretrained('fake/x')",), Source.AGENT_SHORT, "NLP")
    fixed = AgentSource(chat).repair(candidate, ["hf_model:fake/x"], "cpu")

    assert "hf_model:fake/x" in chat.users[0]
    assert fixed is not None
    assert fixed.code == ("import pandas",)
    assert fixed.domain == "NLP"


def test_a_repair_with_no_usable_code_gives_up():
    candidate = Candidate(("x",), Source.AGENT_SHORT, "NLP")
    assert AgentSource(Chat({"code": []})).repair(candidate, ["package:x"], "cpu") is None
