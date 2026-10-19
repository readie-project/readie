"""Normalizing and filtering the ``packages`` a request asks to have installed.

The client validates and dedupes the list too, to fail fast, but a client is
not trusted: this is where the list is authoritatively checked, before any of
it reaches a worker that hands it to ``uv``. The rules are the client's
(``readie.packages.normalize_packages``) and a shared fixture of vectors
(``pkg/tests/data/package_specs.json``) keeps the two in step.

Pure domain, like the rest of ``scheduling/``: no gRPC, no awaiting.
"""

from __future__ import annotations

from collections.abc import Container, Iterable

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from readie_router.errors import InvalidRequestError


def normalize_packages(specs: Iterable[str]) -> tuple[str, ...]:
    """Return ``specs`` deduped by normalized distribution name and sorted.

    Each entry is a PEP 508 requirement string. The first occurrence of a
    distribution wins and later ones naming it, however spelled, are dropped.
    The text of the winner is kept as sent, specifier and extras included.
    Blank entries are skipped. Raises ``InvalidRequestError`` for an entry that
    is not a valid requirement, which also rejects anything that could be read
    as a command-line option.
    """
    seen: dict[str, str] = {}
    for raw in specs:
        text = raw.strip()
        if not text:
            continue
        try:
            requirement = Requirement(text)
        except InvalidRequirement as exc:
            msg = f"{text!r} is not a valid package requirement: {exc}"
            raise InvalidRequestError(msg) from exc
        seen.setdefault(canonicalize_name(requirement.name), text)
    return tuple(text for _, text in sorted(seen.items()))


def without_installed(
    specs: Iterable[str], installed: Container[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split ``specs`` into what still needs installing and what the image has.

    ``installed`` holds the canonical name of each distribution the base image
    carries. A spec naming one of them is dropped whatever version it
    asks for, and reported as the spec text. A spec with extras
    is kept: the base distribution may lack the extra's dependencies, and
    installing it adds them without touching what is already there.
    """
    kept: list[str] = []
    ignored: list[str] = []
    for spec in specs:
        requirement = Requirement(spec)
        if canonicalize_name(requirement.name) not in installed or requirement.extras:
            kept.append(spec)
        else:
            ignored.append(spec)
    return tuple(kept), tuple(ignored)
