"""What an environment has installed, and which distribution provides a name.

``analyze`` records, for each measured package, the distribution that provides
it. The catalogue filters on the same thing for the rootfs a sandbox runs, so
both use this one implementation, and a change to how a distribution is
resolved applies to both.

Run as a script (``python inventory.py``) it prints the inventory of the
interpreter that runs it, as JSON. The capture stage feeds this file to the
*rootfs's own* ``python``, so the inventory is what that interpreter really
imports from, with no guessing about its directories. That is why this module
imports only the standard library, never ``readie_pipeline``: the rootfs does
not have the package. A test pins that.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import distributions, packages_distributions


def canonical_name(name: str) -> str:
    """The PEP 503 normalized form of a distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def provided_distributions() -> dict[str, list[str]]:
    """Top-level import name -> distribution names, for the current environment."""
    return dict(packages_distributions())


def distribution_for(name: str, provided: Mapping[str, list[str]]) -> str:
    """Best-effort PyPI distribution name for ``name``'s top-level segment.

    Diagnostic only: ``loaded_modules``, ``memory_size_mb`` and ``import_time``
    are all measured empirically and never depend on this being right, unlike
    the previous ``Requires-Dist``-based walk, which needed a correct
    distribution name to find a package's declared dependencies at all.
    """
    candidates = provided.get(name.split(".", 1)[0])
    return candidates[0] if candidates else ""


@dataclass(frozen=True, slots=True)
class Inventory:
    """What an environment provides."""

    provided: Mapping[str, list[str]]
    """Top-level import name -> distribution names, from ``packages_distributions``."""

    distributions: frozenset[str]
    """Canonical names of the installed distributions."""

    def to_json(self) -> str:
        """Serialise for the capture stage, which reads it from a subprocess."""
        return json.dumps({"provided": self.provided, "distributions": sorted(self.distributions)})

    @classmethod
    def from_json(cls, text: str) -> Inventory:
        """Read what ``to_json`` wrote. Raises ``ValueError`` on anything else."""
        try:
            raw = json.loads(text)
            return cls(
                provided={str(k): [str(d) for d in v] for k, v in raw["provided"].items()},
                distributions=frozenset(str(d) for d in raw["distributions"]),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            msg = f"not an inventory: {exc}"
            raise ValueError(msg) from exc


def discover_inventory() -> Inventory:
    """List what is installed in the current environment."""
    names = frozenset(
        canonical_name(dist.metadata["Name"]) for dist in distributions() if dist.metadata["Name"]
    )
    return Inventory(provided=provided_distributions(), distributions=names)


if __name__ == "__main__":
    print(discover_inventory().to_json())
