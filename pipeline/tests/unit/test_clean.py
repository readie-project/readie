import json
from pathlib import Path

from readie_pipeline.corpus.clean import (
    RESOURCE_REJECTED,
    RewriteLog,
    clean_corpus,
    merge_resources,
)
from readie_pipeline.corpus.models import Corpus, CorpusItem, Source
from readie_pipeline.corpus.runnable import strip_prelude
from readie_pipeline.corpus.standins import StandIns

DOMAIN = "Data Science"
READ = "import pandas as pd\ntrain = pd.read_csv('../input/titanic/train.csv')\nprint(train.shape)"
GOOD = (
    "import pandas as pd\nimport numpy as np\nimport os\n# [readie] stand-in data\n"
    "os.makedirs('../input/titanic', exist_ok=True)\n"
    "pd.DataFrame({'Age': np.arange(10)}).to_csv('../input/titanic/train.csv')\n"
    "train = pd.read_csv('../input/titanic/train.csv')\nprint(train.shape)"
)
RESOURCES = ("kaggle_dataset:o/titanic", "package:pandas")


class Chat:
    def __init__(self, reply=None):
        self.reply = reply
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        return self.reply if self.reply is not None else json.dumps({"cells": {"0": GOOD}})


def nothing_missing(_resources):
    return []


def item(code, resources=RESOURCES, source=Source.KAGGLE):
    return CorpusItem(tuple(code), DOMAIN, source, resources, ref=f"u/{abs(hash(tuple(code)))}")


def write(tmp_path, items):
    path = tmp_path / "corpus.json"
    Corpus.of(items).save(path)
    return path


def run(path, chat=None, *, verify=nothing_missing, log=None, **kw):
    standins = StandIns(chat) if chat is not None else None
    return clean_corpus(
        path, standins=standins, verify=verify, log=log, on_line=lambda _: None, **kw
    )


def test_notebook_syntax_is_fixed_in_place_and_counted(tmp_path: Path):
    path = write(tmp_path, [item(["%matplotlib inline\nimport numpy as np"])])
    report = run(path)

    assert report.changed == 1
    assert report.fixes["magic:matplotlib"] == 1
    assert Corpus.load(path).items[0].code[0].startswith("# [readie] removed: %matplotlib inline")


def test_an_item_that_reads_a_missing_file_is_rewritten_and_keeps_its_original(tmp_path: Path):
    path = write(tmp_path, [item([READ])])
    report = run(path, Chat())

    stored = Corpus.load(path).items[0]
    assert report.rewrites == {"rewritten": 1}
    assert GOOD in stored.code[0]
    assert stored.original is not None
    assert "pd.read_csv('../input/titanic/train.csv')" in stored.original[0]
    assert report.still_missing == 0


def test_the_dataset_resource_is_kept_when_the_rewrite_no_longer_reads_it(tmp_path: Path):
    # The resource says what the workload is about, so it must outlive the rewrite.
    path = write(tmp_path, [item([READ])])
    run(path, Chat())
    resources = Corpus.load(path).items[0].resources
    assert "kaggle_dataset:o/titanic" in resources
    assert "package:pandas" in resources


def test_imports_a_rewrite_adds_are_unioned_in_after_they_are_checked():
    verified: list[tuple[str, ...]] = []

    def verify(new):
        verified.append(new)
        return []

    merged = merge_resources(RESOURCES, (GOOD,), (0,), verify)
    assert merged == (*RESOURCES[:1], "package:numpy", "package:os", "package:pandas")
    assert verified == [("package:numpy", "package:os")]


def test_a_rewrite_that_imports_something_that_does_not_exist_is_refused(tmp_path: Path):
    path = write(tmp_path, [item([READ])])
    report = run(path, Chat(), verify=lambda _new: ["package:numpy"])

    stored = Corpus.load(path).items[0]
    assert report.rewrites == {RESOURCE_REJECTED: 1}
    assert strip_prelude(stored.code[0]) == READ
    assert stored.original is None


def test_a_rewrite_the_model_got_wrong_keeps_the_original(tmp_path: Path):
    path = write(tmp_path, [item([READ])])
    report = run(path, Chat("not json"))
    assert strip_prelude(Corpus.load(path).items[0].code[0]) == READ
    assert report.still_missing == 1
    assert sum(report.rewrites.values()) == 1


def test_items_that_read_nothing_missing_cost_no_model_call(tmp_path: Path):
    chat = Chat()
    path = write(tmp_path, [item(["x = 1\nprint(x)"])])
    run(path, chat)
    assert chat.calls == 0


def test_mechanical_only_when_no_model_is_given(tmp_path: Path):
    path = write(tmp_path, [item([READ])])
    report = run(path)
    assert report.rewrites == {}
    assert report.still_missing == 1


def test_an_item_already_rewritten_is_not_sent_again(tmp_path: Path):
    path = write(tmp_path, [item([READ])])
    run(path, Chat())
    chat = Chat()
    run(path, chat)
    assert chat.calls == 0


def test_the_log_skips_checked_and_failed_rewrites_on_a_resumed_pass_unless_asked_to_retry(
    tmp_path: Path,
):
    path = write(tmp_path, [item([READ])])
    log_path = tmp_path / "rewrites.json"
    still_reads = json.dumps({"cells": {"0": READ + "\n# changed"}})

    run(path, Chat(still_reads), log=RewriteLog(log_path))
    again = Chat()
    report = run(path, again, log=RewriteLog(log_path))
    assert again.calls == 0
    assert report.already_attempted == 1

    retried = Chat()
    run(path, retried, log=RewriteLog(log_path), retry=True)
    assert retried.calls == 1
    assert Corpus.load(path).items[0].original is not None


def test_a_failed_model_call_is_not_remembered_so_the_next_pass_tries_again(tmp_path: Path):
    path = write(tmp_path, [item([READ])])
    log_path = tmp_path / "rewrites.json"

    run(path, Chat("not json"), log=RewriteLog(log_path))
    again = Chat()
    report = run(path, again, log=RewriteLog(log_path))

    assert again.calls == 1
    assert report.already_attempted == 0
    assert report.rewrites == {"rewritten": 1}


def test_cleaning_twice_changes_nothing_the_second_time(tmp_path: Path):
    path = write(tmp_path, [item(["%matplotlib inline\nimport numpy"]), item([READ])])
    run(path, Chat())
    before = path.read_text()
    report = run(path, Chat())
    assert path.read_text() == before
    assert report.changed == 0


def test_the_original_survives_a_save_and_load_round_trip(tmp_path: Path):
    path = write(tmp_path, [item([READ])])
    run(path, Chat())
    stored = Corpus.load(path).items[0]
    assert stored.original is not None
    assert strip_prelude(stored.original[0]) == READ
    assert "original" in json.loads(path.read_text())[0]
    assert "original" not in CorpusItem(("x = 1",), DOMAIN, Source.AGENT_SHORT).to_json()
