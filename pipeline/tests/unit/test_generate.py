"""The generation flow, with every service replaced by a fake."""

import contextlib
import json
import random
import threading
from pathlib import Path

import pytest

from readie_pipeline.config import ConfigError, CorpusSettings
from readie_pipeline.corpus.generate import (
    Generator,
    generate,
    require_credentials,
    revalidate,
)
from readie_pipeline.corpus.mix import DEFAULT_WEIGHTS
from readie_pipeline.corpus.models import Corpus, CorpusError, CorpusItem, Source
from readie_pipeline.corpus.sources import Candidate
from readie_pipeline.corpus.standins import StandIns
from readie_pipeline.corpus.verify import ResourceVerifier

DOMAIN = "Data Science"


class Origin:
    """A source that hands out a fixed queue of candidates, then runs dry."""

    def __init__(self, queue, repaired=None):
        self.queue = list(queue)
        self.repaired = repaired
        self.requests: list[tuple[Source, str, int, str]] = []

    def fetch(self, source, domain, count, flavor, known_ref):
        self.requests.append((source, domain, count, flavor))
        out = [c for c in self.queue if not known_ref(c.ref)][:count]
        self.queue = [c for c in self.queue if c not in out]
        return out

    def repair(self, candidate, failures, flavor):
        return self.repaired


class Judge:
    def __init__(self, flavors=None, default="cpu"):
        self.flavors = flavors or {}
        self.default = default

    def judge(self, code):
        return self.flavors.get(code[0], self.default)


class Checker:
    def __init__(self, real):
        self.real = real

    def exists(self, kind, value):
        return f"{kind.value}:{value}" in self.real


def cand(code, source=Source.KAGGLE, ref=None, declared=()):
    return Candidate((code,), source, DOMAIN, ref=ref, declared=declared)


class Ticker:
    """A clock that advances by ``step`` seconds every time it is read."""

    def __init__(self, step):
        self.now = 0.0
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


def run(
    origin,
    targets,
    *,
    judge=None,
    real=("package:pandas", "package:numpy"),
    corpus=None,
    flavor="cpu",
    source=Source.KAGGLE,
    clock=None,
    lines=None,
    standins=None,
    clean=True,
):
    saved: list[Corpus] = []
    gen = Generator(
        flavor,
        {source: origin},
        judge or Judge(),
        ResourceVerifier(Checker(set(real)), sleep=lambda _: None),
        checkpoint_every=60.0,
        clock=clock or Ticker(0.0),
        standins=standins,
        clean=clean,
        on_line=(lines.append if lines is not None else lambda _: None),
    )
    grown, report = gen.run(corpus or Corpus.of([]), targets, saved.append, random.Random(0))
    return grown, report, saved


# -- making the code runnable before it is stored -------------------------------
READS_MISSING = (
    "import pandas as pd\ndf = pd.read_csv('../input/titanic/train.csv')\nprint(df.shape)"
)
STANDIN = (
    "import pandas as pd\nimport numpy as np\nimport os\n# [readie] stand-in data\n"
    "os.makedirs('../input/titanic', exist_ok=True)\n"
    "pd.DataFrame({'a': np.arange(5)}).to_csv('../input/titanic/train.csv')\n"
    "df = pd.read_csv('../input/titanic/train.csv')\nprint(df.shape)"
)


class RewriteChat:
    def __init__(self, source=STANDIN):
        self.source = source
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        return json.dumps({"cells": {"0": self.source}})


def test_notebook_syntax_is_fixed_before_the_item_is_stored():
    origin = Origin([cand("%matplotlib inline\nimport pandas as pd", ref="u/nb")])
    grown, report, _ = run(origin, {(DOMAIN, Source.KAGGLE): 1})

    assert grown.items[0].code[0].startswith("# [readie] removed: %matplotlib inline")
    assert report.cleaned == 1


def test_cleaning_can_be_switched_off():
    origin = Origin([cand("%matplotlib inline\nimport pandas as pd")])
    grown, _, _ = run(origin, {(DOMAIN, Source.KAGGLE): 1}, clean=False)
    assert grown.items[0].code == ("%matplotlib inline\nimport pandas as pd",)


def test_a_read_of_a_missing_file_is_rewritten_into_stand_in_data_and_the_original_is_kept():
    origin = Origin([cand(READS_MISSING, ref="u/k", declared=("kaggle_dataset:o/titanic",))])
    chat = RewriteChat()
    grown, report, _ = run(
        origin,
        {(DOMAIN, Source.KAGGLE): 1},
        real=("package:pandas", "package:numpy", "package:os", "kaggle_dataset:o/titanic"),
        standins=StandIns(chat),
    )

    stored = grown.items[0]
    assert "stand-in data" in stored.code[0]
    assert stored.original is not None
    assert "pd.read_csv('../input/titanic/train.csv')" in stored.original[0]
    assert report.rewrites == {"rewritten": 1}


def test_the_dataset_stays_in_the_resources_after_its_reads_are_replaced():
    origin = Origin([cand(READS_MISSING, declared=("kaggle_dataset:o/titanic",))])
    grown, _, _ = run(
        origin,
        {(DOMAIN, Source.KAGGLE): 1},
        real=("package:pandas", "package:numpy", "package:os", "kaggle_dataset:o/titanic"),
        standins=StandIns(RewriteChat()),
    )
    resources = grown.items[0].resources
    assert "kaggle_dataset:o/titanic" in resources
    assert "package:numpy" in resources


def test_a_rewrite_that_imports_something_unreal_keeps_the_original_code():
    origin = Origin([cand(READS_MISSING, declared=("kaggle_dataset:o/titanic",))])
    grown, report, _ = run(
        origin,
        {(DOMAIN, Source.KAGGLE): 1},
        real=("package:pandas", "kaggle_dataset:o/titanic"),  # numpy and os are not "real"
        standins=StandIns(RewriteChat()),
    )

    assert grown.items[0].original is None
    assert "stand-in data" not in grown.items[0].code[0]
    assert report.rewrites == {"resource check failed": 1}


def test_a_model_that_cannot_rewrite_never_loses_the_item():
    class Broken:
        def complete(self, system, user):
            raise RuntimeError("endpoint down")

    origin = Origin([cand(READS_MISSING, declared=("kaggle_dataset:o/titanic",))])
    grown, report, _ = run(
        origin,
        {(DOMAIN, Source.KAGGLE): 1},
        real=("package:pandas", "kaggle_dataset:o/titanic"),
        standins=StandIns(Broken()),
    )
    assert len(grown) == 1
    assert grown.items[0].original is None
    assert report.rewrites == {"model failed": 1}


def test_code_that_reads_nothing_missing_costs_no_rewrite_call():
    chat = RewriteChat()
    origin = Origin([cand("import pandas as pd\nprint(1)")])
    run(origin, {(DOMAIN, Source.KAGGLE): 1}, standins=StandIns(chat))
    assert chat.calls == 0


def test_an_item_that_passes_every_gate_is_added_with_its_resources():
    origin = Origin([cand("import pandas as pd", ref="u/1")])
    grown, report, _ = run(origin, {(DOMAIN, Source.KAGGLE): 1})

    assert [i.resources for i in grown] == [("package:pandas",)]
    assert grown.items[0].ref == "u/1"
    assert report.shortfall == {}


def test_resources_the_origin_declares_are_kept_and_verified():
    origin = Origin([cand("import pandas", declared=("kaggle_dataset:o/d",))])
    grown, report, _ = run(
        origin, {(DOMAIN, Source.KAGGLE): 1}, real=("package:pandas", "kaggle_dataset:o/d")
    )
    assert "kaggle_dataset:o/d" in grown.items[0].resources

    origin = Origin([cand("import pandas", declared=("kaggle_dataset:o/gone",))])
    grown, report, _ = run(origin, {(DOMAIN, Source.KAGGLE): 1})
    assert len(grown) == 0
    assert report.rejected == {"resource does not exist": 1}


def test_code_naming_a_resource_that_does_not_exist_is_rejected():
    origin = Origin([cand("m = AutoModel.from_pretrained('org/imaginary')")])
    grown, report, _ = run(origin, {(DOMAIN, Source.KAGGLE): 1})
    assert len(grown) == 0
    assert report.rejected["resource does not exist"] == 1


class Widening(Origin):
    """Runs dry until asked for new search phrases, as a real source does."""

    def __init__(self, fresh_after_broadening, adds=1, limit=99):
        super().__init__([])
        self.fresh = list(fresh_after_broadening)
        self.adds = adds
        self.limit = limit
        self.broadened: list[list[str]] = []

    def broaden(self, source, domain, flavor, examples):
        self.broadened.append(list(examples))
        if len(self.broadened) > self.limit or not self.adds:
            return 0
        self.queue.extend(self.fresh)
        self.fresh = []
        return self.adds


def test_a_pair_that_runs_dry_is_widened_and_then_finds_more():
    origin = Widening([cand("import pandas", ref="u/new")])
    grown, _, _ = run(origin, {(DOMAIN, Source.KAGGLE): 1})

    assert [i.ref for i in grown] == ["u/new"]
    assert len(origin.broadened) == 1


def test_the_titles_of_what_was_found_are_given_to_the_model_when_widening():
    first = cand("import pandas", ref="https://www.kaggle.com/code/o/house-prices")
    origin = Widening([cand("import numpy", ref="https://www.kaggle.com/code/o/other")])
    origin.queue.append(first)
    run(origin, {(DOMAIN, Source.KAGGLE): 3})
    assert origin.broadened[0] == ["house-prices"]


def test_a_source_that_cannot_add_phrases_is_not_asked_again_and_the_pair_ends():
    origin = Widening([cand("import pandas", ref="u/never")], adds=0)
    grown, report, _ = run(origin, {(DOMAIN, Source.KAGGLE): 2})

    assert len(grown) == 0
    assert report.shortfall == {(DOMAIN, Source.KAGGLE): 2}
    assert len(origin.broadened) == 1  # asked once, got nothing, stopped after the dry rounds


def test_widening_stops_after_a_run_of_rounds_that_find_nothing():
    from readie_pipeline.corpus.generate import _MAX_FRUITLESS_BROADENINGS

    origin = Widening([], adds=1)  # always claims to add phrases but never yields anything
    grown, _, _ = run(origin, {(DOMAIN, Source.KAGGLE): 1})

    assert len(grown) == 0
    assert len(origin.broadened) == _MAX_FRUITLESS_BROADENINGS


def test_a_pair_whose_new_phrases_keep_finding_code_is_widened_until_it_is_full():
    # Pinned: widening used to stop after 10 times per pair however well it was
    # going, so a pair that kept finding code was cut off short of its target.
    from readie_pipeline.corpus.generate import _MAX_FRUITLESS_BROADENINGS

    class Productive(Origin):
        def __init__(self):
            super().__init__([])
            self.broadened = 0

        def broaden(self, source, domain, flavor, examples):
            self.broadened += 1
            self.queue.append(
                cand(f"import pandas\nprint({self.broadened})", ref=f"u/{self.broadened}")
            )
            return 1

    target = 3 * _MAX_FRUITLESS_BROADENINGS
    origin = Productive()
    grown, report, _ = run(origin, {(DOMAIN, Source.KAGGLE): target})

    assert len(grown) == target
    assert report.shortfall == {}


def test_a_source_without_the_ability_to_widen_just_gives_up_as_before():
    grown, report, _ = run(Origin([]), {(DOMAIN, Source.KAGGLE): 1})
    assert len(grown) == 0
    assert report.shortfall == {(DOMAIN, Source.KAGGLE): 1}


class CountingJudge(Judge):
    """Records every call and how many were in flight together."""

    def __init__(self, hold=None, fail_on=()):
        super().__init__()
        self.calls: list[tuple[str, ...]] = []
        self.hold = hold
        self.fail_on = fail_on
        self._lock = threading.Lock()

    def judge(self, code):
        with self._lock:
            self.calls.append(tuple(code))
        if self.hold is not None:
            with contextlib.suppress(threading.BrokenBarrierError):
                self.hold.wait()
        if code[0] in self.fail_on:
            raise CorpusError("model reply is not JSON")
        return "cpu"


def test_the_judge_calls_for_a_batch_run_together():
    # Seen live: judging a batch one candidate at a time took 1.6s each and was
    # most of the time a batch took.
    gate = threading.Barrier(3, timeout=5)
    judge = CountingJudge(hold=gate)
    grown, _, _ = run(Origin(distinct(3)), {(DOMAIN, Source.KAGGLE): 3}, judge=judge)

    assert len(grown) == 3
    assert not gate.broken  # all three were in the judge at the same moment


def test_each_candidate_is_judged_exactly_once():
    judge = CountingJudge()
    run(Origin(distinct(4)), {(DOMAIN, Source.KAGGLE): 4}, judge=judge)
    assert len(judge.calls) == 4
    assert len(set(judge.calls)) == 4


def test_a_candidate_a_cheaper_gate_rejects_is_never_sent_to_the_judge():
    judge = CountingJudge()
    candidates = [cand("def broken(:", ref="bad"), *distinct(2)]
    grown, report, _ = run(Origin(candidates), {(DOMAIN, Source.KAGGLE): 3}, judge=judge)

    assert len(grown) == 2
    assert report.rejected == {"not python": 1}
    assert all("def broken" not in code[0] for code in judge.calls)


def test_a_judge_failure_in_a_batch_rejects_that_candidate_and_keeps_the_rest():
    candidates = distinct(3)
    judge = CountingJudge(fail_on=(candidates[1].code[0],))
    grown, report, _ = run(Origin(candidates), {(DOMAIN, Source.KAGGLE): 3}, judge=judge)

    assert len(grown) == 2
    assert report.rejected == {"judge failed": 1}


def distinct(n):
    return [cand(f"import pandas\nv{i} = {i}", ref=f"r{i}") for i in range(n)]


def test_a_long_pair_saves_its_progress_while_it_runs():
    # Seen live: a pair needing 100+ items from a slow service wrote nothing until
    # it finished, so the run looked frozen for an hour and stopping it lost the lot.
    _, _, saved = run(Origin(distinct(12)), {(DOMAIN, Source.KAGGLE): 12}, clock=Ticker(100.0))

    assert [len(c) for c in saved[:2]] == [5, 10]
    assert len(saved[-1]) == 12


def test_progress_is_not_written_more_often_than_the_interval():
    _, _, saved = run(Origin(distinct(12)), {(DOMAIN, Source.KAGGLE): 12}, clock=Ticker(0.0))
    assert [len(c) for c in saved] == [12]  # only the save when the pair finishes


def test_a_line_says_how_far_along_a_pair_is():
    lines: list[str] = []
    run(Origin(distinct(12)), {(DOMAIN, Source.KAGGLE): 12}, lines=lines)
    assert "    5 of 12 so far" in lines
    assert "    10 of 12 so far" in lines


def test_what_a_pair_found_before_the_run_died_is_already_on_disk():
    class Dies(Origin):
        def fetch(self, source, domain, count, flavor, known_ref):
            if self.requests:
                raise RuntimeError("killed")
            return super().fetch(source, domain, count, flavor, known_ref)

    origin = Dies(distinct(12))
    saved: list[Corpus] = []
    gen = Generator(
        "cpu",
        {Source.KAGGLE: origin},
        Judge(),
        ResourceVerifier(Checker({"package:pandas"}), sleep=lambda _: None),
        checkpoint_every=60.0,
        clock=Ticker(100.0),
        on_line=lambda _: None,
    )
    with pytest.raises(RuntimeError):
        gen.run(Corpus.of([]), {(DOMAIN, Source.KAGGLE): 12}, saved.append, random.Random(0))

    assert [len(c) for c in saved] == [5]


def test_a_malformed_resource_rejects_the_candidate_and_not_the_run():
    # Seen live: a blank declared dataset raised out of the verifier and killed
    # the run at pair 19, discarding everything not yet fetched.
    origin = Origin([cand("import pandas", ref="bad", declared=("kaggle_dataset:",))])
    origin.queue.append(cand("import numpy", ref="good"))
    grown, report, _ = run(origin, {(DOMAIN, Source.KAGGLE): 2})

    assert [i.ref for i in grown] == ["good"]
    assert report.rejected == {"malformed resource": 1}


def test_code_that_does_not_parse_is_rejected():
    grown, report, _ = run(Origin([cand("def broken(:")]), {(DOMAIN, Source.KAGGLE): 1})
    assert len(grown) == 0
    assert report.rejected["not python"] == 1


def test_only_the_runs_flavor_is_kept():
    origin = Origin([cand("import numpy", ref="a"), cand("import pandas", ref="b")])
    judge = Judge({"import numpy": "gpu"})
    grown, report, _ = run(origin, {(DOMAIN, Source.KAGGLE): 2}, judge=judge)

    assert [i.ref for i in grown] == ["b"]
    assert report.rejected["wrong flavor (gpu)"] == 1


def test_a_gpu_run_rejects_cpu_code():
    grown, report, _ = run(
        Origin([cand("import numpy")]), {(DOMAIN, Source.KAGGLE): 1}, flavor="gpu"
    )
    assert len(grown) == 0
    assert report.rejected["wrong flavor (cpu)"] == 1


def test_a_candidate_repeating_the_corpus_is_rejected():
    existing = CorpusItem(("import pandas",), DOMAIN, Source.KAGGLE_NOTEBOOK, ("package:pandas",))
    origin = Origin([cand("import  pandas   # same program", ref="new")])
    grown, report, _ = run(origin, {(DOMAIN, Source.KAGGLE): 1}, corpus=Corpus.of([existing]))
    assert len(grown) == 1
    assert report.rejected["duplicate"] == 1


def test_two_identical_candidates_in_one_run_keep_one():
    origin = Origin([cand("import pandas", ref="a"), cand("import pandas", ref="b")])
    grown, _, _ = run(origin, {(DOMAIN, Source.KAGGLE): 2})
    assert len(grown) == 1


def test_a_shortfall_is_reported_not_backfilled():
    grown, report, _ = run(Origin([cand("import pandas")]), {(DOMAIN, Source.KAGGLE): 3})
    assert len(grown) == 1
    assert report.shortfall == {(DOMAIN, Source.KAGGLE): 2}


def test_pairs_already_at_target_are_not_fetched():
    existing = CorpusItem(("import pandas",), DOMAIN, Source.KAGGLE, ("package:pandas",))
    origin = Origin([cand("import numpy")])
    _, _, _ = run(origin, {(DOMAIN, Source.KAGGLE): 1}, corpus=Corpus.of([existing]))
    assert origin.requests == []


def test_the_corpus_is_saved_after_each_pair():
    origin = Origin([cand("import pandas")])
    _, _, saved = run(origin, {(DOMAIN, Source.KAGGLE): 1})
    assert [len(c) for c in saved] == [1]


def test_a_lookup_outage_drops_the_candidate_without_calling_it_fake():
    class Down:
        def exists(self, kind, value):
            from readie_pipeline.corpus.verify import ResourceLookupError

            raise ResourceLookupError("503")

    gen = Generator(
        "cpu",
        {Source.KAGGLE: Origin([cand("import pandas")])},
        Judge(),
        ResourceVerifier(Down(), attempts=1, sleep=lambda _: None),
        on_line=lambda _: None,
    )
    grown, report = gen.run(
        Corpus.of([]), {(DOMAIN, Source.KAGGLE): 1}, lambda _: None, random.Random(0)
    )
    assert len(grown) == 0
    assert report.rejected["lookup failed"] == 1


def test_a_fetch_that_raises_is_counted_and_the_run_continues():
    class Broken(Origin):
        def fetch(self, *args):
            raise OSError("network")

    grown, report, _ = run(Broken([]), {(DOMAIN, Source.KAGGLE): 1})
    assert len(grown) == 0
    assert report.rejected["fetch failed"] == 3


# -- generated code ---------------------------------------------------------
def test_generated_code_with_a_fake_resource_is_repaired_once():
    bad = cand("m = AutoModel.from_pretrained('org/imaginary')", source=Source.AGENT_SHORT)
    good = cand("import pandas", source=Source.AGENT_SHORT)
    origin = Origin([bad], repaired=good)
    grown, report, _ = run(origin, {(DOMAIN, Source.AGENT_SHORT): 1}, source=Source.AGENT_SHORT)

    assert [i.resources for i in grown] == [("package:pandas",)]
    assert report.rejected == {}


def test_a_repair_that_is_still_wrong_is_rejected():
    bad = cand("m = AutoModel.from_pretrained('org/imaginary')", source=Source.AGENT_SHORT)
    origin = Origin([bad], repaired=cand("import nope_pkg", source=Source.AGENT_SHORT))
    grown, report, _ = run(origin, {(DOMAIN, Source.AGENT_SHORT): 1}, source=Source.AGENT_SHORT)
    assert len(grown) == 0
    assert report.rejected["resource does not exist"] == 1


def test_fetched_code_is_never_repaired():
    origin = Origin(
        [cand("m = AutoModel.from_pretrained('org/imaginary')")], repaired=cand("import pandas")
    )
    grown, _, _ = run(origin, {(DOMAIN, Source.KAGGLE): 1})
    assert len(grown) == 0


# -- the whole entry point --------------------------------------------------
def settings(**overrides):
    base = {"endpoint": "e", "api_key": "k", "model": "m", "total": 14, "seed": 1}
    return CorpusSettings(**{**base, **overrides})


def test_generate_builds_a_mixed_corpus_and_resumes(tmp_path: Path):
    n = iter(range(10_000))

    class Endless:
        def fetch(self, source, domain, count, flavor, known_ref):
            return [
                Candidate((f"import pandas\nv{next(n)} = 1",), source, domain, ref=f"r{next(n)}")
                for _ in range(count)
            ]

    sources = {s: Endless() for s in Source}
    path = tmp_path / "cpu.json"
    verifier = ResourceVerifier(Checker({"package:pandas"}), tmp_path / "c.json")
    first, report = generate(
        settings(),
        "cpu",
        path,
        tmp_path / "c.json",
        sources=sources,
        judge=Judge(),
        verifier=verifier,
        on_line=lambda _: None,
    )

    assert len(first) == 14
    assert set(first.source_counts()) == {s.value for s in Source}
    assert report.shortfall == {}

    # Pinned: ``total`` once meant "items to hold", so a second run added nothing and
    # a larger target had to be worked out by hand. A run appends ``total`` more.
    again, report = generate(
        settings(),
        "cpu",
        path,
        tmp_path / "c.json",
        sources=sources,
        judge=Judge(),
        verifier=verifier,
        on_line=lambda _: None,
    )
    assert len(again) == 28
    assert again.items[:14] == first.items
    assert sum(report.added.values()) == 14
    assert json.loads(path.read_text())


def test_generate_can_be_limited_to_a_source_and_domain(tmp_path: Path):
    class Endless:
        def fetch(self, source, domain, count, flavor, known_ref):
            return [Candidate((f"import pandas\nv{i} = 1",), source, domain) for i in range(count)]

    corpus, _ = generate(
        settings(total=3),
        "cpu",
        tmp_path / "x.json",
        tmp_path / "c.json",
        sources={Source.AGENT_SHORT: Endless()},
        judge=Judge(),
        verifier=ResourceVerifier(Checker({"package:pandas"})),
        only_sources=(Source.AGENT_SHORT,),
        only_domains=(DOMAIN,),
        on_line=lambda _: None,
    )
    assert corpus.source_counts() == {"agent_short": 3}
    assert corpus.domain_counts() == {DOMAIN: 3}


def test_generate_skips_what_an_excluded_corpus_already_holds(tmp_path: Path):
    class Counting:
        made = 0

        def fetch(self, source, domain, count, flavor, known_ref):
            start, self.made = self.made, self.made + count
            return [
                Candidate((f"import pandas\nv{i} = 1",), source, domain)
                for i in range(start, self.made)
            ]

    held = Candidate(("import pandas\nv0 = 1",), Source.AGENT_SHORT, DOMAIN)
    other = tmp_path / "other.json"
    Corpus.of([CorpusItem(held.code, DOMAIN, Source.AGENT_SHORT, ("package:pandas",))]).save(other)

    corpus, _ = generate(
        settings(total=2),
        "cpu",
        tmp_path / "x.json",
        tmp_path / "c.json",
        sources={Source.AGENT_SHORT: Counting()},
        judge=Judge(),
        verifier=ResourceVerifier(Checker({"package:pandas"})),
        only_sources=(Source.AGENT_SHORT,),
        only_domains=(DOMAIN,),
        exclude=(other,),
        on_line=lambda _: None,
    )
    assert len(corpus) == 2
    assert held.code not in [item.code for item in corpus.items]


def test_an_unknown_domain_is_rejected_before_anything_is_fetched(tmp_path: Path):
    with pytest.raises(CorpusError, match="unknown domain 'Astrology'"):
        generate(
            settings(),
            "cpu",
            tmp_path / "x.json",
            tmp_path / "c.json",
            only_domains=("Astrology",),
        )


def test_revalidate_drops_items_that_no_longer_resolve(tmp_path: Path):
    path = tmp_path / "cpu.json"
    Corpus.of(
        [
            CorpusItem(("import pandas",), DOMAIN, Source.KAGGLE, ("package:pandas",)),
            CorpusItem(("import ghost_pkg",), DOMAIN, Source.KAGGLE, ("package:ghost_pkg",)),
            CorpusItem(("def broken(:",), DOMAIN, Source.KAGGLE, ()),
        ]
    ).save(path)

    kept = revalidate(path, ResourceVerifier(Checker({"package:pandas"})), on_line=lambda _: None)

    assert [i.code for i in kept] == [("import pandas",)]
    assert len(Corpus.load(path)) == 1


# -- credentials ------------------------------------------------------------
def test_kaggle_sources_need_a_kaggle_credential():
    with pytest.raises(ConfigError, match="KAGGLE_API_TOKEN"):
        require_credentials(settings(hf_token="t"), DEFAULT_WEIGHTS)


def test_hugging_face_sources_need_a_token():
    with pytest.raises(ConfigError, match="HF_TOKEN"):
        require_credentials(settings(kaggle_token="t"), DEFAULT_WEIGHTS)


def test_a_disabled_source_needs_no_credential():
    weights = {s: (1.0 if s.value.startswith("agent_") else 0.0) for s in Source}
    require_credentials(settings(), weights)
