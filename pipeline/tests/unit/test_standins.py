import json

from readie_pipeline.corpus.runnable import clean_steps, split_prelude
from readie_pipeline.corpus.standins import Outcome, StandIns

READ = "import pandas as pd\ntrain = pd.read_csv('../input/titanic/train.csv')\nprint(train.shape)"
GOOD = (
    "import pandas as pd\nimport numpy as np\nimport os\n"
    "# [readie] stand-in data\n"
    "os.makedirs('../input/titanic', exist_ok=True)\n"
    "_rng = np.random.default_rng(0)\n"
    "pd.DataFrame({'Age': _rng.normal(30, 10, 100)}).to_csv('../input/titanic/train.csv')\n"
    "train = pd.read_csv('../input/titanic/train.csv')\nprint(train.shape)"
)


class Chat:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.replies.pop(0) if self.replies else "{}"


def reply(**cells):
    return json.dumps({"cells": {k.removeprefix("c"): v for k, v in cells.items()}})


def test_code_that_reads_nothing_missing_is_left_alone_without_a_model_call():
    chat = Chat()
    result = StandIns(chat).rewrite(["x = 1\nprint(x)"])
    assert result.outcome is Outcome.NOTHING_TO_DO
    assert chat.calls == []


def test_a_cell_that_reads_a_missing_file_is_replaced_by_the_models_version():
    chat = Chat(reply(c0=GOOD))
    result = StandIns(chat).rewrite([READ])
    assert result.outcome is Outcome.REWRITTEN
    assert result.code == (GOOD,)
    assert result.cells == (0,)


def test_only_flagged_cells_are_replaced_and_the_others_are_byte_identical():
    cells = ["import os", READ, "print('done')"]
    chat = Chat(reply(c1=GOOD, c0="import sys", c2="print('changed')"))
    result = StandIns(chat).rewrite(cells)
    assert result.code == ("import os", GOOD, "print('done')")


def test_the_prompt_shows_every_cell_and_names_the_files_to_fix():
    chat = Chat(reply(c1=GOOD))
    StandIns(chat).rewrite(["import os", READ])
    system, user = chat.calls[0]
    assert "### cell 0\nimport os" in user
    assert "cell 1: ../input/titanic/train.csv" in user
    assert "EXACTLY the path" in system


def test_a_rewrite_that_still_reads_a_missing_file_is_asked_again_with_the_reason():
    still_missing = READ + "\n# changed"
    chat = Chat(reply(c0=still_missing), reply(c0=GOOD))
    result = StandIns(chat).rewrite([READ])

    assert result.outcome is Outcome.REWRITTEN
    assert "still reads ../input/titanic/train.csv" in chat.calls[1][1]


def test_two_failed_attempts_keep_the_original():
    still_missing = READ + "\n# changed"
    chat = Chat(reply(c0=still_missing), reply(c0=still_missing))
    result = StandIns(chat).rewrite([READ])
    assert result.outcome is Outcome.CHECK_FAILED
    assert result.code == (READ,)


def test_a_reply_that_is_not_json_or_has_no_cells_keeps_the_original():
    for bad in ("sorry, I cannot", "{}", '{"cells": []}'):
        result = StandIns(Chat(bad, bad)).rewrite([READ])
        assert result.code == (READ,)
        assert result.outcome in {Outcome.MODEL_FAILED, Outcome.CHECK_FAILED}


def test_a_cell_that_does_not_parse_is_rejected():
    result = StandIns(Chat(reply(c0="def broken(:"), reply(c0="def broken(:"))).rewrite([READ])
    assert result.outcome is Outcome.CHECK_FAILED
    assert result.code == (READ,)


def test_an_unchanged_cell_is_rejected():
    assert StandIns(Chat(reply(c0=READ), reply(c0=READ))).rewrite([READ]).code == (READ,)


def test_an_item_larger_than_the_cap_is_left_as_it_is_without_a_model_call():
    chat = Chat()
    result = StandIns(chat, max_chars=50).rewrite([READ])
    assert result.outcome is Outcome.TOO_LARGE
    assert chat.calls == []


def test_the_prelude_is_hidden_from_the_model_and_put_back_on_the_rewritten_cell():
    steps = clean_steps((READ,)).code
    assert steps[0].startswith("# [readie:prelude]")
    chat = Chat(reply(c0=GOOD))

    result = StandIns(chat).rewrite(steps)

    assert "[readie:prelude]" not in chat.calls[0][1]
    prelude, body = split_prelude(result.code[0])
    assert prelude.startswith("# [readie:prelude]")
    assert body == GOOD
