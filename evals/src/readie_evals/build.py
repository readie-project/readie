"""Building the corpus: fetch, adapt, and fill the deficit.

For each (source, category) short of its target, this sources raw candidates and
turns them into tasks -- generation writes them directly, the fetchers' output is
adapted by the model. It appends to the corpus as it goes and only ever fills the
shortfall, so an interrupted build continues on the next invocation.

``sample_corpus`` is the other way in: it ignores what the corpus already has and
always adds ``count`` fresh tasks, spread evenly across randomly ordered
(source, category) pairs rather than filling out a grid, the way an unplanned user
session would mix real fetches, fresh generation and session tasks. A pair may be
drawn more than once -- nothing is unique about the pair, only about the code a
draw produces.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence

from readie_evals.agent.adapt import adapt_candidate, adapt_session_candidate
from readie_evals.agent.generate import generate_session_tasks, generate_tasks
from readie_evals.agent.ports import LLM, Fetcher
from readie_evals.catalogue import warm_imports
from readie_evals.config import Settings
from readie_evals.corpus import CorpusStore
from readie_evals.fetch import FetchError
from readie_evals.models import Task
from readie_evals.taxonomy import GENERATED_SESSION_SOURCES, REAL_SESSION_SOURCES


def _targets(
    sources: Sequence[str],
    categories: Sequence[str],
    per_cell: int,
) -> dict[tuple[str, str], int]:
    return {(source, category): per_cell for source in sources for category in categories}


def build_corpus(
    settings: Settings,
    llm: LLM,
    fetchers: dict[str, Fetcher],
    corpus: CorpusStore,
    *,
    sources: Sequence[str],
    categories: Sequence[str],
    on_line: Callable[[str], None] = print,
) -> int:
    """Fill the corpus toward ``per_cell`` per (source, category). Returns tasks added.

    ``fetchers`` maps a source name to its fetcher; a source with no fetcher (and
    that is not ``generated``) is skipped with a note.
    """
    warm = warm_imports(settings.catalogue_path)
    deficit = corpus.deficit(_targets(sources, categories, settings.per_cell))
    added = 0
    for (source, category), need in sorted(deficit.items()):
        on_line(f"[*] {source}/{category}: need {need}")
        tasks = _tasks_for(
            source, category, need, llm=llm, fetchers=fetchers, warm=warm, on_line=on_line
        )
        added += corpus.append(tasks)
    on_line(f"[=] added {added} task(s)")
    return added


#: Safety valve on sample_corpus's total attempts, as a multiple of ``count``.
#: A pair can be tried again in a later round (see the function docstring), so
#: without a cap a source that always fails (bad credentials, a dead fetch
#: target) would spin forever instead of finishing best-effort.
_MAX_ATTEMPT_FACTOR = 20


def sample_corpus(
    settings: Settings,
    llm: LLM,
    fetchers: dict[str, Fetcher],
    corpus: CorpusStore,
    *,
    sources: Sequence[str],
    categories: Sequence[str],
    count: int,
    on_line: Callable[[str], None] = print,
    rng: random.Random | None = None,
) -> int:
    """Add ``count`` fresh tasks, spread evenly across (source, category) pairs.

    Unlike ``build_corpus``, this never looks at what the corpus already holds: a
    re-run adds a fresh, independent batch rather than topping up toward a target.
    "No repetition" means no two tasks share the same code, not that a
    (source, category) pair is used only once -- corpus.append() already rejects
    identical code (tasks are keyed by a hash of source and code), so a pair can be
    drawn as many times as needed as long as each draw's own fetch/generation
    happens to differ.

    Pairs are drawn in shuffled rounds -- one full, re-shuffled pass over the grid
    per round -- rather than a single shuffle tried once each, so a long sample
    keeps spreading across every (source, category) roughly evenly instead of
    clustering on whichever pairs happen to keep working. A pair that yields
    nothing (a fetch failure, an unusable snippet) is skipped for this round and
    may come up again in a later one; the whole draw is best-effort and gives up
    after `_MAX_ATTEMPT_FACTOR` times ``count`` attempts, in case a source is
    reliably broken (e.g. missing credentials) rather than just having bad luck.
    """
    rng = rng if rng is not None else random.Random()  # noqa: S311 - picking a task order, not a secret
    warm = warm_imports(settings.catalogue_path)
    base_pairs = [(source, category) for source in sources for category in categories]
    max_attempts = count * _MAX_ATTEMPT_FACTOR

    added = 0
    attempts = 0
    while added < count and attempts < max_attempts:
        round_pairs = list(base_pairs)
        rng.shuffle(round_pairs)
        for source, category in round_pairs:
            if added >= count or attempts >= max_attempts:
                break
            attempts += 1
            on_line(f"[*] sampling {source}/{category}")
            try:
                tasks = _tasks_for(
                    source, category, 1, llm=llm, fetchers=fetchers, warm=warm, on_line=on_line
                )
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - see below
                # A single bad draw must not take down a long, otherwise-healthy
                # sample: an LLM hiccup, a flaky fetch, or (observed from the
                # kaggle client specifically) a dependency that calls sys.exit()
                # instead of raising on a missing credential all land here rather
                # than aborting every pair after them.
                on_line(f"    {source}/{category} raised {exc!r}, trying another pair")
                continue
            appended = corpus.append(tasks)
            if appended == 0:
                on_line(f"    no usable task for {source}/{category}, trying another pair")
            added += appended

    if added < count:
        on_line(f"[!] sampled {added}/{count}: gave up after {attempts} attempts")
    else:
        on_line(f"[=] sampled {added} task(s)")
    return added


def _tasks_for(
    source: str,
    category: str,
    need: int,
    *,
    llm: LLM,
    fetchers: dict[str, Fetcher],
    warm: frozenset[str],
    on_line: Callable[[str], None],
) -> list[Task]:
    if source == "generated":
        return generate_tasks(llm, category=category, count=need, warm=warm, on_line=on_line)
    if source in GENERATED_SESSION_SOURCES:
        kind = source.removeprefix("generated-")
        return generate_session_tasks(
            llm, category=category, count=need, warm=warm, kind=kind, on_line=on_line
        )

    real_session = source in REAL_SESSION_SOURCES
    base_source = source.removesuffix("-notebook") if real_session else source
    fetcher = fetchers.get(base_source)
    if fetcher is None:
        on_line(f"    no fetcher for {base_source}; install readie-evals[fetch]")
        return []
    try:
        candidates = fetcher.fetch(category=category, count=need)
    except FetchError as exc:
        on_line(f"    fetch failed: {exc}")
        return []

    tasks: list[Task] = []
    for candidate in candidates:
        task = (
            adapt_session_candidate(llm, candidate, warm=warm, source=source)
            if real_session
            else adapt_candidate(llm, candidate, warm=warm)
        )
        if task is not None:
            tasks.append(task)
        else:
            on_line(f"    skipped an unadaptable {source} candidate")
    return tasks
