"""How mainstream a piece of code is, and how often to ask for each kind.

A corpus drawn only from the top of a popularity ranking is a few tutorials
repeated; one drawn only from the tail is a pile of curiosities. Real traffic
is mostly mainstream with a long tail, so every source picks a tier for each
request and the tiers are weighted toward the head.
"""

from __future__ import annotations

import random
from enum import StrEnum


class Tier(StrEnum):
    """How widely used the code a request asks for should be."""

    MAINSTREAM = "mainstream"
    COMMON = "common"
    NICHE = "niche"


#: Share of requests per tier. Mainstream dominates because that is what most
#: users and agents run; niche is kept so the long tail is still represented.
TIER_WEIGHTS: dict[Tier, float] = {
    Tier.MAINSTREAM: 0.55,
    Tier.COMMON: 0.30,
    Tier.NICHE: 0.15,
}


def pick_tier(rng: random.Random) -> Tier:
    """Choose a tier for one request."""
    tiers = list(TIER_WEIGHTS)
    return rng.choices(tiers, weights=[TIER_WEIGHTS[t] for t in tiers])[0]
