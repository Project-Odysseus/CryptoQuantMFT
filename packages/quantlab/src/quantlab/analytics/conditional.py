"""Performance conditional on a regime label (plan 3.6): every signal's report gets a by-regime section.

- `by_regime`: annualised return, Sharpe and share of bars of each strategy on the bars carrying each label.
- `after_changes`: average cumulative P&L in the first n bars after the label changes, per strategy.

Labels are known at the close of t and P&L is that of bar t (positions already lag by the engine), so a label is
compared with the P&L earned while it held.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np
import pandas as pd


def by_regime(pnl: Mapping[str, pd.Series], labels: pd.Series, *, periods: float = 252.0) -> pd.DataFrame:
    """Rows: (strategy, statistic); columns: labels."""
    states = [state for state in pd.unique(labels.dropna())]
    rows = {}
    for name, series in pnl.items():
        aligned = series.reindex(labels.index)
        for state in states:
            chunk = aligned[labels == state].dropna()
            spread = chunk.std()
            rows[(name, "annual return")] = {**rows.get((name, "annual return"), {}), state: float(chunk.mean() * periods)}
            rows[(name, "Sharpe")] = {**rows.get((name, "Sharpe"), {}), state: float(chunk.mean() / spread * np.sqrt(periods)) if len(chunk) > 20 and spread > 0 else float("nan")}
    out = pd.DataFrame(rows).T
    shares = labels.value_counts(normalize=True)
    out.loc[("all", "share of bars"), :] = [float(shares.get(state, 0.0)) for state in out.columns]
    return out


def after_changes(pnl: Mapping[str, pd.Series], labels: pd.Series, horizons: Sequence[int] = (1, 3, 5, 10)) -> pd.DataFrame:
    """Mean cumulative P&L (in basis points of capital) over the first n bars after each label change."""
    valid = labels.dropna()
    changes = valid.index[valid.ne(valid.shift()).to_numpy()][1:]
    out = {}
    for name, series in pnl.items():
        values = series.reindex(labels.index).fillna(0.0)
        positions = values.index.get_indexer(changes)
        out[name] = {f"{horizon} bars": float(np.mean([values.iloc[p + 1:p + 1 + horizon].sum() for p in positions if p + horizon < len(values)]) * 1e4)
                     for horizon in horizons}
    return pd.DataFrame(out).T
