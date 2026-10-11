"""Decide how many items each (domain, source) pair should contribute.

The corpus is meant to cover every domain and every source, in proportions the
operator sets. The schedule is computed up front and is deterministic for a
given seed, so an interrupted run resumes against the same targets.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

from readie_pipeline.corpus.models import CorpusError, Source

#: Real code is weighted above generated code by default, because it is what
#: agents and users actually run. Hugging Face is weighted below Kaggle because
#: the Hub holds far less Python code per repository, so its share is the one a
#: run most often cannot fill.
DEFAULT_WEIGHTS: dict[Source, float] = {
    Source.KAGGLE: 2.0,
    Source.KAGGLE_NOTEBOOK: 2.0,
    Source.HUGGINGFACE: 1.5,
    Source.HUGGINGFACE_NOTEBOOK: 1.5,
    Source.AGENT_SHORT: 1.0,
    Source.AGENT_NOTEBOOK: 1.0,
    Source.AGENT_FIX: 1.0,
}


def parse_weights(text: str) -> dict[Source, float]:
    """Read ``source=weight,source=weight`` over the defaults.

    Raises:
        CorpusError: A source name is unknown or a weight is not a number >= 0.
    """
    weights = dict(DEFAULT_WEIGHTS)
    for part in filter(None, (p.strip() for p in text.split(","))):
        name, _, raw = part.partition("=")
        try:
            source = Source(name.strip())
            weight = float(raw)
        except ValueError as exc:
            msg = f"bad source weight {part!r}; use <source>=<number>"
            raise CorpusError(msg) from exc
        if weight < 0:
            msg = f"source weight for {name} must not be negative"
            raise CorpusError(msg)
        weights[source] = weight
    if not any(w > 0 for w in weights.values()):
        msg = "every source weight is zero"
        raise CorpusError(msg)
    return weights


def source_targets(total: int, weights: Mapping[Source, float]) -> dict[Source, int]:
    """Split ``total`` across sources by weight, using largest remainders."""
    active = {s: w for s, w in weights.items() if w > 0}
    scale = sum(active.values())
    exact = {s: total * w / scale for s, w in active.items()}
    counts = {s: int(v) for s, v in exact.items()}
    leftover = total - sum(counts.values())
    for source in sorted(exact, key=lambda s: exact[s] - counts[s], reverse=True)[:leftover]:
        counts[source] += 1
    return counts


def schedule(
    total: int,
    weights: Mapping[Source, float],
    domains: Sequence[str],
    seed: int | None = None,
) -> dict[tuple[str, Source], int]:
    """Target count for every (domain, source) pair.

    Within a source the domains are dealt out round robin from a shuffled
    order, so no domain is favoured and every one appears once a source has
    at least as many items as there are domains.
    """
    rng = random.Random(seed)  # noqa: S311 - not security sensitive
    targets: dict[tuple[str, Source], int] = {}
    for source, count in source_targets(total, weights).items():
        order = list(domains)
        rng.shuffle(order)
        for i in range(count):
            key = (order[i % len(order)], source)
            targets[key] = targets.get(key, 0) + 1
    return targets


def remaining(
    targets: Mapping[tuple[str, Source], int],
    have: Mapping[tuple[str, Source], int],
    rng: random.Random,
) -> list[tuple[tuple[str, Source], int]]:
    """Pairs still short of their target, in random order, with how many are missing."""
    short = [(key, want - have.get(key, 0)) for key, want in targets.items()]
    needed = [(key, n) for key, n in short if n > 0]
    rng.shuffle(needed)
    return needed
