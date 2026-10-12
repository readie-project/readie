"""Replace reads of files that do not exist with generated stand-in data.

Fetched notebooks read datasets that were attached to them on Kaggle. Nothing in
a sandbox has those files, so the first read fails and the notebook never gets to
the work that makes it a workload. The model is asked to write, immediately
before each such read, code that creates a small stand-in file with the same
columns and types at exactly the path the code reads, and to leave every other
line alone. The results mean nothing; the amount of work and the memory it uses
stay in the right range.

Only the cells that read missing files are rewritten, and a rewrite is accepted
only when a static check agrees that nothing it reads is still missing. Anything
else keeps the original code.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from readie_pipeline.corpus.dependencies import missing_inputs
from readie_pipeline.corpus.llm import Chat, parse_json
from readie_pipeline.corpus.models import CorpusError
from readie_pipeline.corpus.runnable import parses, split_prelude

#: Items whose code is longer than this are left as they are, since the model is
#: shown all of it for context.
DEFAULT_MAX_CHARS = 60_000
_ATTEMPTS = 2

_SYSTEM = """\
You make Python code runnable in an isolated sandbox that has no network and none
of the data files the code expects. You are given the cells of one notebook or
script. Some cells read files that do not exist. Reply with a single JSON object
{"cells": {"<cell number>": "<complete new source of that cell>"}} and nothing else.

For every cell you are told to fix, return its complete new source in which each
missing file is created first, with generated stand-in data, at EXACTLY the path
the code reads, immediately before the first line that reads it. Leave every other
line of the cell exactly as it is: do not rename anything, reformat, reorder or
remove code, and keep its imports.

Rules for the stand-in data:
- Write it with a literal path string identical to the one the code reads, and
  create the folder first: os.makedirs(os.path.dirname(path), exist_ok=True).
  Use the writer that matches the format (to_csv, to_json, np.save, open(..., 'w')).
- Work out the columns, their types and value ranges from how this cell and the
  cells after it use the data (column names, astype, groupby, the target of a fit,
  date parsing). Use a typical size for that kind of data: hundreds to a few
  thousand rows, never more than 50,000, and no file over about 20 MB.
- Generate it with numpy's default_rng(0) (and pandas), so it is repeatable.
  Include every column the code uses, including identifiers and the target.
- For folders of images or audio, write a few small generated files per class.
- For formats the code loads with a library (npy, pickle, h5, joblib, model
  weights), write a small compatible file.
- Do not use the network or any library that is not already imported or standard.
- Put the comment `# [readie] stand-in data` on the line before the generated block.
- Only return the cells you were told to fix."""


class Outcome(StrEnum):
    """What happened to one item."""

    REWRITTEN = "rewritten"
    NOTHING_TO_DO = "nothing to rewrite"
    TOO_LARGE = "too large"
    MODEL_FAILED = "model failed"
    CHECK_FAILED = "check failed"


@dataclass(frozen=True, slots=True)
class Result:
    """The code to keep, and why."""

    outcome: Outcome
    code: tuple[str, ...]
    cells: tuple[int, ...] = ()


class StandIns:
    """Rewrites an item's missing-file reads into stand-in data with the hosted model."""

    def __init__(self, chat: Chat, *, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        self._chat = chat
        self._max_chars = max_chars

    def rewrite(self, code: Sequence[str]) -> Result:
        """The rewritten steps, or the original ones with the reason they were kept."""
        original = tuple(code)
        flagged = missing_inputs(original)
        if not flagged:
            return Result(Outcome.NOTHING_TO_DO, original)
        if sum(len(step) for step in original) > self._max_chars:
            return Result(Outcome.TOO_LARGE, original)

        problems: list[str] = []
        outcome = Outcome.MODEL_FAILED
        for _ in range(_ATTEMPTS):
            try:
                reply = parse_json(
                    self._chat.complete(_SYSTEM, _prompt(original, flagged, problems))
                )
            except CorpusError as exc:
                problems = [f"your reply could not be used: {exc}"]
                outcome = Outcome.MODEL_FAILED
                continue
            candidate, problems = _apply(original, flagged, reply)
            if not problems:
                return Result(Outcome.REWRITTEN, candidate, tuple(sorted(flagged)))
            outcome = Outcome.CHECK_FAILED
        return Result(outcome, original)


def _prompt(code: tuple[str, ...], flagged: dict[int, list[str]], problems: list[str]) -> str:
    parts = ["CELLS, numbered from 0:"]
    for index, step in enumerate(code):
        parts.append(f"### cell {index}\n{split_prelude(step)[1]}")
    parts.append("CELLS YOU MUST FIX, with the files they read that do not exist:")
    parts.extend(f"cell {i}: {', '.join(paths)}" for i, paths in sorted(flagged.items()))
    if problems:
        parts.append("YOUR PREVIOUS REPLY WAS REJECTED, FIX THESE: " + "; ".join(problems))
    parts.append('Reply as {"cells": {"<number>": "<complete new source>"}}.')
    return "\n\n".join(parts)


def _apply(
    original: tuple[str, ...], flagged: dict[int, list[str]], reply: object
) -> tuple[tuple[str, ...], list[str]]:
    """The code with the model's cells swapped in, and what is wrong with them."""
    cells = reply.get("cells") if isinstance(reply, dict) else None
    if not isinstance(cells, dict):
        return original, ['the reply must be {"cells": {...}}']

    steps = list(original)
    problems: list[str] = []
    for index in sorted(flagged):
        new = cells.get(str(index))
        if not isinstance(new, str) or not new.strip():
            problems.append(f"cell {index} is missing from the reply")
            continue
        if not parses(new):
            problems.append(f"cell {index} is not valid Python")
            continue
        prelude, body = split_prelude(original[index])
        if new == body:
            problems.append(f"cell {index} is unchanged")
            continue
        steps[index] = prelude + new

    if problems:
        return original, problems
    still = missing_inputs(steps)
    if still:
        shown = "; ".join(f"cell {i} still reads {', '.join(p)}" for i, p in sorted(still.items()))
        return original, [shown + " (create each file with a literal path before reading it)"]
    return tuple(steps), []
