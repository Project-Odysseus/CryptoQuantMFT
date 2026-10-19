"""What each instrument earns per bar, and what trading it costs, built from the instrument master (plan 2.3).

- **Index proxy or index:** price return + (carry yield - its short rate) / periods, the excess return of a future on
  it. The rate comes point-in-time from an `oecd` series (`rate:<area>` in the master), known from the next month.
- **Future with held-contract data:** pass its `held_returns` (quantlab.data.futures) instead; the price is not used.
- **Perp, spot, FX rate:** price return. Perp funding is a separate cash flow (a later item).

Costs come from the master: `cost_bps` per side, or `cost_ticks` x tick size / price (a cost that changes as the
price moves), giving bps per bar for the engine.
"""

from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd

from quantlab.data.catalog import DataCatalog
from quantlab.data.instruments import InstrumentMaster
from quantlab.data.panel import Panel, asof_daily


def short_rates(master: InstrumentMaster, areas: set[str], index: pd.DatetimeIndex, catalog: DataCatalog) -> dict[str, pd.Series]:
    """Each area's short rate (a fraction a year) as known on each date of `index`."""
    out = {}
    for area in sorted(areas):
        adapter, symbol = master[f"rate:{area}"].source("1M")
        frame, _snapshot = catalog.get(adapter, symbol, "1M")
        out[area] = asof_daily(frame, index)
    return out


def excess_returns(panel: Panel, master: InstrumentMaster, *, catalog: DataCatalog | None = None, rates: Mapping[str, pd.Series] | None = None,
                   periods: float = 252.0) -> pd.DataFrame:
    """Per-bar returns for every instrument of a panel (dates x instruments), excess of cash where that applies.

    `rates` (area -> daily series) overrides fetching the short rates. Bars where an instrument is stale or not yet
    listed earn nothing new: a carried price has a zero price return (and still carries).
    """
    close = panel["close"]
    price_return = close.pct_change(fill_method=None)
    needs_rate = {master[instrument].rate_area for instrument in panel.instruments if master[instrument].kind in ("index", "future") and master[instrument].rate_area}
    if rates is None and needs_rate:
        rates = short_rates(master, needs_rate, panel.index, catalog or DataCatalog())
    out = {}
    for instrument in panel.instruments:
        spec = master[instrument]
        series = price_return[instrument]
        if spec.kind in ("index", "future") and spec.rate_area:
            rate = (rates or {})[spec.rate_area].reindex(panel.index)
            series = series + (spec.carry_yield - rate) / periods
        out[instrument] = series.where(close[instrument].notna() & close[instrument].shift(1).notna())
    return pd.DataFrame(out, index=panel.index)


def cost_table(panel: Panel, master: InstrumentMaster) -> pd.DataFrame:
    """Cost per side in basis points per bar (dates x instruments): cost_ticks x tick / price when set, else cost_bps."""
    out = {}
    for instrument in panel.instruments:
        spec = master[instrument]
        if spec.cost_ticks:
            out[instrument] = spec.cost_ticks * spec.tick_size / panel["close"][instrument] * 10_000.0
        else:
            out[instrument] = pd.Series(spec.cost_bps, index=panel.index)
    return pd.DataFrame(out, index=panel.index).replace([np.inf, -np.inf], np.nan).ffill()
