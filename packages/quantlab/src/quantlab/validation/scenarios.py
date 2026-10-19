"""Historical scenarios and hypothetical shocks (plan 4.6).

A Sharpe ratio averages over good and bad times; a fund needs to know what the bad times look like.

- `historical(returns)`: return, worst drawdown, volatility and worst day in each named crisis (`HISTORICAL`),
  next to a benchmark's when given. Windows the data does not cover are left out.
- `stress(held, returns)`: the strategy's positions held as they were, against altered market returns:
  - "volatility x2": every market's deviation from its mean doubled;
  - "correlations to one": every market moves with one common factor (each keeps its own volatility), so
    diversification between positions disappears and only the net, volatility-weighted exposure matters;
  - "gap": a one-day move of `gap_sigma` trailing standard deviations against the book in every market at once,
    evaluated on every day's positions: the worst and the 1% worst.
  Positions are not allowed to react, so these are instant losses before any rule would cut risk.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np
import pandas as pd

HISTORICAL: dict[str, tuple[str, str]] = {
    "1987 crash": ("1987-10-01", "1987-10-30"),
    "1994 bond rout": ("1994-02-01", "1994-11-30"),
    "1998 LTCM": ("1998-07-17", "1998-10-08"),
    "2000-02 dot-com bust": ("2000-03-24", "2002-10-09"),
    "2007-09 financial crisis": ("2007-10-09", "2009-03-09"),
    "2010 flash crash / euro": ("2010-04-23", "2010-07-02"),
    "2011 US downgrade": ("2011-07-22", "2011-10-03"),
    "2015 China deval": ("2015-08-10", "2015-08-31"),
    "2018 volatility spike": ("2018-01-26", "2018-02-08"),
    "2018 Q4 sell-off": ("2018-10-03", "2018-12-24"),
    "2020 Covid crash": ("2020-02-19", "2020-03-23"),
    "2022 rates shock": ("2022-01-03", "2022-10-12"),
}


def _window_stats(piece: pd.Series) -> dict[str, float]:
    growth = (1.0 + piece.fillna(0.0)).cumprod()
    return {"return": float(growth.iloc[-1] - 1.0), "max drawdown": float((growth / growth.cummax() - 1.0).min()),
            "volatility (annual)": float(piece.std() * math.sqrt(252.0)), "worst day": float(piece.min())}


def historical(returns: pd.Series, *, benchmark: pd.Series | None = None, scenarios: Mapping[str, tuple[str, str]] = HISTORICAL) -> pd.DataFrame:
    """The strategy (and benchmark) in each crisis window the data covers."""
    rows = {}
    returns = returns.dropna()
    for name, (start, end) in scenarios.items():
        if returns.empty or pd.Timestamp(start) < returns.index[0] or pd.Timestamp(end) > returns.index[-1]:
            continue
        row = _window_stats(returns.loc[start:end])
        if benchmark is not None:
            row["benchmark return"] = _window_stats(benchmark.loc[start:end])["return"]
        rows[name] = row
    return pd.DataFrame(rows).T


def _sharpe_and_drawdown(pnl: pd.Series) -> dict[str, float]:
    growth = (1.0 + pnl).cumprod()
    spread = pnl.std()
    return {"Sharpe": float(pnl.mean() / spread * math.sqrt(252.0)) if spread > 0 else float("nan"),
            "max drawdown": float((growth / growth.cummax() - 1.0).min()), "worst day": float(pnl.min())}


def stress(held: pd.DataFrame, returns: pd.DataFrame, *, vol_factor: float = 2.0, gap_sigma: float = 5.0, vol_window: int = 63) -> dict[str, Any]:
    """The book under each shock (see the module docstring): a table, and the gap loss on every day."""
    held = held.reindex(columns=returns.columns).fillna(0.0)
    market = returns.reindex(held.index).fillna(0.0)
    base = (held * market).sum(axis=1)
    rows = {"as it was": _sharpe_and_drawdown(base)}
    mean = market.mean()
    rows[f"volatility x{vol_factor:g}"] = _sharpe_and_drawdown((held * ((market - mean) * vol_factor + mean)).sum(axis=1))
    sigma = market.rolling(vol_window, min_periods=20).std().shift(1)
    standardised = (market - mean) / market.std()
    factor = standardised.mean(axis=1)
    factor = factor / factor.std()
    common = pd.DataFrame(np.outer(factor.to_numpy(), market.std().to_numpy()), index=market.index, columns=market.columns) + mean
    rows["correlations to one"] = _sharpe_and_drawdown((held * common).sum(axis=1))
    exposure = (held * sigma).sum(axis=1)  # P&L of a +1 sigma move in every market
    gap = -gap_sigma * exposure.abs()  # the move goes against the net exposure
    table = pd.DataFrame(rows).T
    table.loc[f"gap of {gap_sigma:g} sigma, worst day", "worst day"] = float(gap.min())
    table.loc[f"gap of {gap_sigma:g} sigma, 1% worst", "worst day"] = float(gap.quantile(0.01))
    return {"table": table, "gap loss": gap}
