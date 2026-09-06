"""Normalizing the ``packages=`` list on ``@remote``.

The executor installs these with ``uv`` before every call. Two entries that
name the same distribution -- ``"Requests"`` and ``"requests"``, or
``"scikit_learn"`` and ``"scikit-learn"`` -- must dedupe to one, which is what
PEP 503 name normalization (lowercase, runs of ``-._`` collapsed to a single
``-``) is for. A version specifier or extras, if present, rides through
unchanged -- normalization only ever touches the distribution name.
"""

from __future__ import annotations

from collections.abc import Iterable

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from readie.errors import InvalidPackageError


def normalize_packages(specs: Iterable[str]) -> tuple[str, ...]:
    """Return ``specs``, deduped by normalized distribution name and sorted.

    Each entry is a requirement string (``"numpy"``, ``"numpy==1.26.0"``,
    ``"requests[security]>=2.31"``). The first occurrence of a given
    distribution wins; later ones naming the same package -- however they are
    spelled -- are dropped rather than kept alongside a possibly conflicting
    specifier.
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
            raise InvalidPackageError(msg) from exc
        key = canonicalize_name(requirement.name)
        seen.setdefault(key, text)
    return tuple(text for _, text in sorted(seen.items()))
