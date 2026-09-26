"""Netting: several sleeves on one instrument become one position, and each sleeve keeps its share.

If one sleeve wants +0.3 of equity in BTC and another -0.1, the book holds
+0.2 and trades once. Fees are paid on the net change only. Attribution keeps
each sleeve's contribution so its P&L can still be reported as if it traded
alone.
"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd


def net_targets(sleeve_targets: Mapping[str, tuple[str, float]]) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Sum sleeve weights per instrument.

    Args:
        sleeve_targets: Sleeve id to (instrument id, allocated weight).

    Returns:
        (net weight per instrument, per instrument the weight each sleeve contributed).
    """
    net: dict[str, float] = {}
    attribution: dict[str, dict[str, float]] = {}
    for sleeve, (instrument, weight) in sleeve_targets.items():
        net[instrument] = net.get(instrument, 0.0) + float(weight)
        attribution.setdefault(instrument, {})[sleeve] = float(weight)
    return net, attribution


def net_history(sleeve_weights: pd.DataFrame, sleeve_instrument: Mapping[str, str]) -> pd.DataFrame:
    """Sum allocated sleeve weight histories (columns = sleeve ids) into instrument weight histories."""
    grouped = sleeve_weights.T.groupby(lambda sleeve: sleeve_instrument[sleeve]).sum().T
    return grouped.reindex(sleeve_weights.index).fillna(0.0)
