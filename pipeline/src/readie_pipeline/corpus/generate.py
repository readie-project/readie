"""Build or extend a corpus for one flavor.

The run turns a target mix (how many items per domain and source) into work:
for each (domain, source) pair still short of its target, ask the source for
candidates and put every one through the same gates before it is kept:

1. not a duplicate of anything already accepted;
2. valid Python in every step;
3. judged to belong to this run's flavor;
4. every resource it uses exists, verified against the service that owns it.

The corpus is saved after every pair, and at least once a minute while a large
pair is still running, so an interrupted run resumes against the same targets and
keeps nearly everything it earned.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

from readie_pipeline.config import ConfigError, CorpusSettings
from readie_pipeline.corpus.clean import RESOURCE_REJECTED, merge_resources
from readie_pipeline.corpus.dedupe import DuplicateIndex
from readie_pipeline.corpus.domains import DOMAINS
from readie_pipeline.corpus.extract import CodeError, extract_resources
from readie_pipeline.corpus.flavor import Classifier
from readie_pipeline.corpus.mix import parse_weights, remaining, schedule
from readie_pipeline.corpus.models import Corpus, CorpusError, CorpusItem, Source
from readie_pipeline.corpus.runnable import Cleaned, clean_steps
from readie_pipeline.corpus.sources import Broadens, Candidate, CodeSource, Repairer
from readie_pipeline.corpus.standins import Outcome, Result, StandIns
from readie_pipeline.corpus.verify import ResourceLookupError, ResourceVerifier

#: How many consecutive fetches for one pair may add nothing before giving up on it.
_MAX_DRY_ROUNDS = 3
#: Empty rounds in a row before a source is asked for new search phrases, and how
#: many times in a row it may be asked without that producing a single item. A pair
#: that keeps finding code keeps being widened; one whose new phrases find nothing
#: stops after this many. Each widening resets the empty-round count.
_BROADEN_AFTER = 2
_MAX_FRUITLESS_BROADENINGS = 5
#: Flavor-judge calls for one batch of candidates run together, this many at a time.
_JUDGE_WORKERS = 6


@dataclass(slots=True)
class Report:
    """What a run did, for the operator."""

    added: dict[tuple[str, Source], int] = field(default_factory=dict)
    shortfall: dict[tuple[str, Source], int] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)
    rewrites: dict[str, int] = field(default_factory=dict)
    cleaned: int = 0

    def reject(self, reason: str) -> None:
        """Count one rejected candidate."""
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


class Generator:
    """Runs the gates over what the sources return."""

    def __init__(
        self,
        flavor: str,
        sources: Mapping[Source, CodeSource],
        judge: Classifier,
        verifier: ResourceVerifier,
        *,
        batch_size: int = 5,
        checkpoint_every: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        standins: StandIns | None = None,
        clean: bool = True,
        rewrite_workers: int = 6,
        on_line: Callable[[str], None] = print,
    ) -> None:
        self._standins = standins
        self._clean = clean
        self._rewrite_workers = rewrite_workers
        self._checkpoint_every = checkpoint_every
        self._judged: dict[tuple[str, ...], str | CorpusError] = {}
        self._clock = clock
        self._flavor = flavor
        self._sources = sources
        self._judge = judge
        self._verifier = verifier
        self._batch_size = batch_size
        self._on_line = on_line

    def run(
        self,
        corpus: Corpus,
        targets: Mapping[tuple[str, Source], int],
        save: Callable[[Corpus], None],
        rng: random.Random,
        exclude: Iterable[CorpusItem] = (),
    ) -> tuple[Corpus, Report]:
        """Fill every pair toward its target and return the grown corpus.

        Items in ``exclude`` are not part of the corpus but count as duplicates, so a
        corpus grown on the side repeats nothing from another one.
        """
        report = Report()
        index = DuplicateIndex.of([*exclude, *corpus.items])
        pending = remaining(targets, corpus.cell_counts(), rng)
        for number, ((domain, source), need) in enumerate(pending, start=1):
            self._on_line(f"[*] ({number}/{len(pending)}) {source.value} / {domain}: need {need}")
            base = corpus

            def checkpoint(kept: list[CorpusItem], base: Corpus = base) -> None:
                save(Corpus.of([*base.items, *kept]))

            fresh = self._fill(domain, source, need, index, report, checkpoint=checkpoint)
            if fresh:
                corpus = Corpus.of([*corpus.items, *fresh])
                save(corpus)
            report.added[(domain, source)] = len(fresh)
            if len(fresh) < need:
                report.shortfall[(domain, source)] = need - len(fresh)
            self._on_line(f"    +{len(fresh)} ({len(corpus)} total)")
        return corpus, report

    def _fill(
        self,
        domain: str,
        source: Source,
        need: int,
        index: DuplicateIndex,
        report: Report,
        *,
        checkpoint: Callable[[list[CorpusItem]], None],
    ) -> list[CorpusItem]:
        """Gather items for one pair, saving progress as they arrive.

        A pair that needs a hundred items from a slow service takes tens of
        minutes. Writing only when it finished meant a run looked frozen for that
        long, and stopping it mid-pair lost everything the pair had found. So the
        corpus is written at most once per ``checkpoint_every`` seconds while the
        pair runs, and a line says how far along it is.
        """
        origin = self._sources[source]
        last_save = self._clock()
        kept: list[CorpusItem] = []
        dry = 0
        fruitless = 0
        while len(kept) < need and dry < _MAX_DRY_ROUNDS:
            try:
                candidates = origin.fetch(
                    source,
                    domain,
                    min(need - len(kept), self._batch_size),
                    self._flavor,
                    index.seen_ref,
                )
            except (CorpusError, OSError) as exc:
                self._on_line(f"    fetch failed: {exc}")
                report.reject("fetch failed")
                dry += 1
                continue

            before = len(kept)
            admitted: list[CorpusItem] = []
            self._prejudge(candidates, index)
            for candidate in candidates:
                # A source may return more than it was asked for. Everything that
                # passes the gates is kept: a pair going over its target is fine.
                item = self._admit(candidate, origin, index, report)
                if item is not None:
                    index.add(item.code, item.ref)
                    admitted.append(item)
            for item in self._finish_all(admitted, report):
                index.add(item.code, item.ref)
                kept.append(item)
            dry = 0 if len(kept) > before else dry + 1
            if len(kept) > before:
                self._on_line(f"    {len(kept)} of {need} so far")
                if self._clock() - last_save >= self._checkpoint_every:
                    checkpoint(kept)
                    last_save = self._clock()
            if len(kept) > before:
                fruitless = 0
            if dry == _BROADEN_AFTER and fruitless < _MAX_FRUITLESS_BROADENINGS:
                fruitless += 1
                if self._broaden(origin, source, domain, kept):
                    dry = 0
        return kept

    def _broaden(
        self, origin: CodeSource, source: Source, domain: str, kept: list[CorpusItem]
    ) -> bool:
        """Ask a source that can widen its search for new phrases; whether it added any."""
        if not isinstance(origin, Broadens):
            return False
        titles = [i.ref.rsplit("/", 1)[-1].split("#")[0] for i in kept[-12:] if i.ref]
        added = origin.broaden(source, domain, self._flavor, titles)
        if added:
            self._on_line(f"    no new code found; the model added {added} search phrases")
        return added > 0

    def _admit(
        self, candidate: Candidate, origin: CodeSource, index: DuplicateIndex, report: Report
    ) -> CorpusItem | None:
        verdict = self._screen(candidate, index)
        if isinstance(verdict, str):
            report.reject(verdict)
            return None

        resources, absent = verdict
        if absent:
            # One repair attempt; the rewrite goes through every gate again.
            repaired = self._repair(candidate, origin, absent)
            second = self._screen(repaired, index) if repaired else None
            if repaired is None or second is None or isinstance(second, str) or second[1]:
                report.reject("resource does not exist")
                return None
            candidate, resources = repaired, second[0]

        return CorpusItem(
            candidate.code, candidate.domain, candidate.source, resources, candidate.ref
        )

    def _prejudge(self, candidates: list[Candidate], index: DuplicateIndex) -> None:
        """Judge the candidates that will need it all at once.

        Each call to the model takes a second or two and the calls do not depend on
        each other, so judging a batch one by one spent most of a batch's time
        waiting. Candidates a cheaper gate would reject (duplicates, code that does
        not parse) are left out, so no call is made for them.
        """
        self._judged.clear()
        worth_judging = [
            c for c in candidates if not index.is_duplicate(c.code, c.ref) and _parses(c)
        ]
        if len(worth_judging) < 2:  # noqa: PLR2004 - one call gains nothing from a pool
            return
        with ThreadPoolExecutor(max_workers=_JUDGE_WORKERS) as pool:
            outcomes = list(pool.map(self._judge_once, worth_judging))
        for candidate, outcome in zip(worth_judging, outcomes, strict=True):
            self._judged[candidate.code] = outcome

    def _judge_once(self, candidate: Candidate) -> str | CorpusError:
        try:
            return self._judge.judge(candidate.code)
        except CorpusError as exc:
            return exc

    def _flavor_of(self, candidate: Candidate) -> str:
        """The flavor, from the batch's earlier judgement when there is one.

        Raises:
            CorpusError: The model's answer could not be used.
        """
        judged = self._judged.pop(candidate.code, None)
        if isinstance(judged, CorpusError):
            raise judged
        return judged if judged is not None else self._judge.judge(candidate.code)

    def _screen(
        self, candidate: Candidate, index: DuplicateIndex
    ) -> str | tuple[tuple[str, ...], list[str]]:
        """Run the gates; return a rejection reason, or the resources and any that do not exist."""
        if index.is_duplicate(candidate.code, candidate.ref):
            return "duplicate"
        try:
            resources = tuple(sorted({*extract_resources(candidate.code), *candidate.declared}))
        except CodeError:
            return "not python"

        try:
            flavor = self._flavor_of(candidate)
        except CorpusError as exc:
            self._on_line(f"    judge failed: {exc}")
            return "judge failed"
        if flavor != self._flavor:
            return f"wrong flavor ({flavor})"

        absent = self._missing(resources)
        if isinstance(absent, str):
            return absent
        return resources, absent

    def _missing(self, resources: tuple[str, ...]) -> list[str] | str:
        """The resources that do not exist, or a rejection reason when none can be checked."""
        try:
            return self._verifier.missing(resources)
        except CorpusError as exc:
            # A resource string that is not ``<kind>:<value>``: a defect in this
            # candidate, never a reason to abandon the run.
            self._on_line(f"    malformed resource: {exc}")
            return "malformed resource"
        except ResourceLookupError as exc:
            # Not evidence the resource is fake, so the candidate is dropped for
            # this run without poisoning the cache.
            self._on_line(f"    lookup failed: {exc}")
            return "lookup failed"

    def _finish_all(self, items: list[CorpusItem], report: Report) -> list[CorpusItem]:
        """Make each admitted item runnable before it is stored.

        The mechanical fixes are instant. The rewrite of missing-file reads is a
        model call per item, so the calls for a batch run together, like the
        flavor judgements. Checking the imports a rewrite adds touches the
        verification cache, so that happens here on one thread, afterwards.
        """
        if not self._clean or not items:
            return items
        with ThreadPoolExecutor(max_workers=self._rewrite_workers) as pool:
            prepared = list(pool.map(self._prepare, items))

        finished: list[CorpusItem] = []
        for item, (cleaned, result) in zip(items, prepared, strict=True):
            report.cleaned += cleaned.code != item.code
            finished.append(self._accept_rewrite(replace(item, code=cleaned.code), result, report))
        return finished

    def _prepare(self, item: CorpusItem) -> tuple[Cleaned, Result | None]:
        cleaned = clean_steps(item.code)
        if self._standins is None:
            return cleaned, None
        try:
            return cleaned, self._standins.rewrite(cleaned.code)
        except CorpusError:
            return cleaned, Result(Outcome.MODEL_FAILED, cleaned.code)
        except Exception:  # noqa: BLE001 - a failed rewrite must never lose the item
            return cleaned, Result(Outcome.MODEL_FAILED, cleaned.code)

    def _accept_rewrite(
        self, item: CorpusItem, result: Result | None, report: Report
    ) -> CorpusItem:
        if result is None or result.outcome is Outcome.NOTHING_TO_DO:
            return item
        outcome = result.outcome.value
        if result.outcome is Outcome.REWRITTEN:
            resources = merge_resources(item.resources, result.code, result.cells, self._missing)
            if resources is None:
                outcome = RESOURCE_REJECTED
            else:
                report.rewrites[outcome] = report.rewrites.get(outcome, 0) + 1
                return replace(item, code=result.code, resources=resources, original=item.code)
        report.rewrites[outcome] = report.rewrites.get(outcome, 0) + 1
        return item

    def _repair(
        self, candidate: Candidate, origin: CodeSource, absent: list[str]
    ) -> Candidate | None:
        if candidate.source.fetched or not isinstance(origin, Repairer):
            return None
        try:
            return origin.repair(candidate, absent, self._flavor)
        except CorpusError:
            return None


def generate(
    settings: CorpusSettings,
    flavor: str,
    corpus_path: Path,
    cache_path: Path,
    *,
    sources: Mapping[Source, CodeSource] | None = None,
    judge: Classifier | None = None,
    verifier: ResourceVerifier | None = None,
    total: int | None = None,
    only_sources: tuple[Source, ...] = (),
    only_domains: tuple[str, ...] = (),
    known_packages: frozenset[str] = frozenset(),
    standins: StandIns | None = None,
    exclude: Sequence[Path] = (),
    on_line: Callable[[str], None] = print,
) -> tuple[Corpus, Report]:
    """Append ``total`` new items to the corpus for ``flavor``.

    Corpus files in ``exclude`` are read once at the start; nothing the run adds
    repeats an item in them.

    The corpus is only ever added to. ``total`` is how many items to add, shared out
    over the sources by weight and over the domains, on top of what each pair
    already holds.

    The collaborators are injected so a test runs the whole flow with fakes;
    left as ``None`` they are built from ``settings`` against the real services.
    """
    weights = parse_weights(settings.source_weights)
    if only_sources:
        weights = {s: (w if s in only_sources else 0.0) for s, w in weights.items()}
    domains = only_domains or DOMAINS
    unknown = [d for d in domains if d not in DOMAINS]
    if unknown:
        msg = f"unknown domain {unknown[0]!r}; choose from {', '.join(DOMAINS)}"
        raise CorpusError(msg)

    share = schedule(total or settings.total, weights, domains, settings.seed)
    if sources is None or judge is None or verifier is None:
        from readie_pipeline.corpus.live import build  # noqa: PLC0415 - optional extra

        live = build(
            settings, weights, cache_path, seed=settings.seed, known_packages=known_packages
        )
        sources = sources or live.sources
        judge = judge or live.judge
        verifier = verifier or live.verifier
        standins = standins or live.standins

    corpus = Corpus.load(corpus_path) if corpus_path.exists() else Corpus.of([])
    have = corpus.cell_counts()
    targets = {key: have.get(key, 0) + n for key, n in share.items()}
    generator = Generator(
        flavor,
        sources,
        judge,
        verifier,
        batch_size=settings.batch_size,
        standins=standins if settings.rewrite else None,
        rewrite_workers=settings.rewrite_workers,
        on_line=on_line,
    )
    rng = random.Random(settings.seed)  # noqa: S311 - not security sensitive
    grown, report = generator.run(
        corpus,
        targets,
        lambda c: c.save(corpus_path),
        rng,
        exclude=[item for path in exclude for item in Corpus.load(path).items],
    )
    verifier.flush()
    return grown, report


def require_credentials(settings: CorpusSettings, weights: Mapping[Source, float]) -> None:
    """Fail clearly when an active source lacks the credential it needs."""
    active = {s for s, w in weights.items() if w > 0}
    if active & {Source.KAGGLE, Source.KAGGLE_NOTEBOOK} and not settings.kaggle_token:
        msg = "Kaggle sources are enabled but KAGGLE_API_TOKEN is not set"
        raise ConfigError(msg)
    if active & {Source.HUGGINGFACE, Source.HUGGINGFACE_NOTEBOOK} and not settings.hf_token:
        msg = "Hugging Face sources are enabled but HF_TOKEN is not set"
        raise ConfigError(msg)


def revalidate(
    corpus_path: Path,
    verifier: ResourceVerifier,
    *,
    on_line: Callable[[str], None] = print,
) -> Corpus:
    """Re-check every item's resources and drop the ones that no longer resolve.

    Resources are re-derived from the code, so a corpus edited by hand, or built
    before a verification rule existed, is brought up to the current standard.
    """
    corpus = Corpus.load(corpus_path)
    kept: list[CorpusItem] = []
    for item in corpus:
        try:
            resources = tuple(sorted({*extract_resources(item.code), *item.resources}))
            absent = verifier.missing(resources)
        except CodeError:
            on_line("    dropped an item whose code does not parse")
            continue
        if absent:
            on_line(f"    dropped {item.source.value} item: {', '.join(absent)}")
            continue
        kept.append(CorpusItem(item.code, item.domain, item.source, resources, item.ref))

    updated = Corpus.of(kept)
    updated.save(corpus_path)
    verifier.flush()
    on_line(f"[*] kept {len(updated)} of {len(corpus)} items -> {corpus_path}")
    return updated


def _parses(candidate: Candidate) -> bool:
    try:
        extract_resources(candidate.code)
    except CodeError:
        return False
    return True
