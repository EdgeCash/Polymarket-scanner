"""Last season's ratings, the prior each team starts the new season from.

``scanner/priors.json`` is built by ``scripts/build_priors.py`` from the previous
season's finished games (ESPN scoreboards, the same source as the log): one entry
per sport with the season it describes, every team's final offence and defence
rating by ESPN team id, and a default for a team it does not know (a newcomer,
or a visitor from outside the division, rated like the weakest tenth of the
set). The projection shrinks a team toward its share of that rating instead of
toward average, so a 25-point favourite is not a 3-point favourite in November.

College basketball's entry is a forecast instead (``scripts/build_roster_priors.py``):
each side's rating as its roster predicts it, from who is back, who has arrived
and what they produced last season. It is marked ``weighted`` and the projection
takes it whole, where the others are taken at the sport's prior weight.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

PRIORS_PATH = Path(__file__).with_name("priors.json")


@lru_cache(maxsize=1)
def _load() -> dict:
    try:
        return json.loads(PRIORS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def reload() -> None:
    _load.cache_clear()


def priors_for(sport: str, season: int | None) -> dict | None:
    """The sport's prior for ``season``: the entry for the season before it, or None.

    A prior from any other season is left unused: two seasons back says little,
    and a prior from the season being rated would count every game twice.
    """
    entry = _load().get(sport)
    if not entry or season is None:
        return None
    try:
        if int(entry.get("season")) != int(season) - 1:
            return None
    except (TypeError, ValueError):
        return None
    return entry
