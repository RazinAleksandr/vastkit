"""Offer ranking.

Hourly price alone is a bad signal on Vast: some hosts charge $10+/TB for
bandwidth, which dominates the bill when a job starts by pulling 30-100 GB of
model weights. Ranking therefore defaults to *effective session cost*:

    session_cost = dph_total * hours + download_gb * bandwidth_$_per_gb
"""

from __future__ import annotations

from typing import List

from .models import Offer

SORT_MODES = ("effective", "price", "speed", "value")


def rank_offers(
    offers: List[Offer],
    sort: str = "effective",
    hours: float = 1.0,
    download_gb: float = 50.0,
) -> List[Offer]:
    """Return offers sorted best-first according to ``sort``.

    - ``effective``: lowest estimated session cost (rent + download) first
    - ``price``:     lowest $/hr first
    - ``speed``:     highest dlperf first
    - ``value``:     highest dlperf per estimated session dollar first
    """
    if sort not in SORT_MODES:
        raise ValueError(f"unknown sort {sort!r} (expected one of {SORT_MODES})")

    def session(o: Offer) -> float:
        return o.session_cost(hours=hours, download_gb=download_gb)

    if sort == "price":
        key, reverse = lambda o: (o.dph_total, -o.dlperf), False
    elif sort == "speed":
        key, reverse = lambda o: (o.dlperf, -o.dph_total), True
    elif sort == "value":
        key, reverse = lambda o: (o.dlperf / max(session(o), 1e-9), -o.dph_total), True
    else:  # effective
        key, reverse = lambda o: (session(o), -o.dlperf), False

    return sorted(offers, key=key, reverse=reverse)
