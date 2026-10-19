"""Search phrases for the real-code sources, seeded by hand and grown by the model.

A fixed list of phrases reaches only the top results of a few dozen searches, and
a pair of (domain, source) that needs hundreds of items runs out of unseen results
long before it runs out of code that exists. When a pair runs dry the source asks
the model for new phrases, shown the ones already tried and the titles of what was
found, and keeps whatever it returns. The learned phrases are saved beside the
verification cache, so a later run starts from them instead of from the seeds.
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from readie_pipeline.corpus.llm import Chat, parse_json
from readie_pipeline.corpus.models import CorpusError

#: How many phrases one expansion asks for, and the bounds on a usable phrase.
PER_EXPANSION = 20
_MIN_LENGTH = 2
_MAX_LENGTH = 40
#: Longest phrase, in words, kept per platform. Kaggle needs every word to match, so
#: a long phrase finds almost nothing; the prompt asks for short ones and this drops
#: the rest.
_MAX_WORDS = {"kaggle": 2}
_SHOWN_TRIED = 80
_SHOWN_EXAMPLES = 12

_SYSTEM = """\
You write search phrases for finding existing Python code on a public site. Reply
with a single JSON object {"terms": ["...", "..."]} and nothing else. Every phrase
is lowercase, short as the site's guidance below asks, and different from the
others and from the phrases you are told were already tried. Prefer breadth: cover different
techniques, libraries, datasets and problem types, not rewordings of one idea."""

#: What each platform's search actually matches, so the phrases suit it.
_PLATFORM_GUIDANCE = {
    "kaggle": (
        "The site is Kaggle. Its notebook search matches notebook titles, descriptions "
        "and tags, and every word of a phrase must match, so each added word cuts the "
        "results sharply: a single word such as 'outlier' matches hundreds of notebooks "
        "and a long phrase usually matches none. Write mostly single words and at most "
        "two-word phrases: algorithms, techniques, library names, and dataset or "
        "competition names, such as 'arima', 'churn', 'titanic', or 'house prices'."
    ),
    "huggingface": (
        "The site is the Hugging Face Hub. Its search matches repository NAMES only, "
        "never code or descriptions, so a long phrase finds nothing. Write short tokens "
        "that appear inside repository names: library names, model families, task "
        "names, and dataset names, such as 'sklearn', 'tabular', 'bert-base', or "
        "'time-series'."
    ),
}

_FLAVOR_GUIDANCE = {
    "cpu": "Find code that runs on a machine without a GPU.",
    "gpu": "Find code that needs and uses a CUDA GPU.",
}


class Keywords(Protocol):
    """What a source needs from its search phrases."""

    def pick(self, platform: str, domain: str, flavor: str, rng: random.Random) -> str:
        """A phrase to search with."""
        ...

    def broaden(self, platform: str, domain: str, flavor: str, examples: Sequence[str] = ()) -> int:
        """Add phrases for the domain; return how many new ones were added."""
        ...


class KeywordBank:
    """Seed phrases per platform and domain, plus the phrases the model added."""

    def __init__(
        self,
        seeds: Mapping[str, Mapping[str, Sequence[str]]],
        chat: Chat | None = None,
        path: Path | None = None,
        per_expansion: int = PER_EXPANSION,
    ) -> None:
        self._seeds = seeds
        self._chat = chat
        self._path = path
        self._per_expansion = per_expansion
        self._learned = self._load()
        self._tried: set[tuple[str, str, str, str]] = set()

    def _load(self) -> dict[str, dict[str, dict[str, list[str]]]]:
        if self._path is None or not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text())
        except json.JSONDecodeError:
            return {}
        return raw if isinstance(raw, dict) else {}

    def _flush(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".partial")
        temp.write_text(json.dumps(self._learned, indent=2, sort_keys=True) + "\n")
        temp.replace(self._path)

    def terms(self, platform: str, domain: str, flavor: str) -> tuple[str, ...]:
        """Every phrase known for this platform, domain and flavor, seeds first."""
        learned = self._learned.get(platform, {}).get(flavor, {}).get(domain, [])
        seeds = self._seeds.get(platform, {}).get(domain, ())
        return tuple(dict.fromkeys([*seeds, *learned]))

    def pick(self, platform: str, domain: str, flavor: str, rng: random.Random) -> str:
        """A phrase not yet used in this run when there is one, else any."""
        terms = self.terms(platform, domain, flavor)
        fresh = [t for t in terms if (platform, flavor, domain, t) not in self._tried]
        term = rng.choice(fresh or list(terms))
        self._tried.add((platform, flavor, domain, term))
        return term

    def broaden(self, platform: str, domain: str, flavor: str, examples: Sequence[str] = ()) -> int:
        """Ask the model for new phrases and keep the usable ones.

        Returns how many were added: ``0`` when there is no model, the call failed,
        or every phrase it gave was already known.
        """
        if self._chat is None:
            return 0
        known = self.terms(platform, domain, flavor)
        try:
            reply = parse_json(
                self._chat.complete(
                    _SYSTEM, self._prompt(platform, domain, flavor, known, examples)
                )
            )
        except CorpusError:
            return 0

        proposed = reply.get("terms") if isinstance(reply, dict) else None
        seen = {t.lower() for t in known}
        added: list[str] = []
        for raw in proposed if isinstance(proposed, list) else []:
            term = _clean(raw, _MAX_WORDS.get(platform))
            if term and term.lower() not in seen:
                seen.add(term.lower())
                added.append(term)

        if added:
            stored = self._learned.setdefault(platform, {}).setdefault(flavor, {})
            stored.setdefault(domain, []).extend(added)
            self._flush()
        return len(added)

    def _prompt(
        self,
        platform: str,
        domain: str,
        flavor: str,
        known: Sequence[str],
        examples: Sequence[str],
    ) -> str:
        lines = [
            _PLATFORM_GUIDANCE[platform],
            _FLAVOR_GUIDANCE[flavor],
            f"Topic: {domain}.",
            f"Write {self._per_expansion} new phrases.",
            "Already tried, do not repeat or reword: " + ", ".join(known[-_SHOWN_TRIED:]),
        ]
        if examples:
            lines.append(
                "Titles of code already found for this topic, for inspiration: "
                + ", ".join(examples[:_SHOWN_EXAMPLES])
            )
        lines.append('Reply as {"terms": [...]}.')
        return "\n".join(lines)


def _clean(raw: object, max_words: int | None = None) -> str | None:
    """A phrase fit to search with, or ``None``."""
    if not isinstance(raw, str):
        return None
    term = " ".join(raw.replace('"', " ").replace("'", " ").split()).strip().lower()
    if max_words is not None and len(term.split()) > max_words:
        return None
    return term if _MIN_LENGTH <= len(term) <= _MAX_LENGTH else None
