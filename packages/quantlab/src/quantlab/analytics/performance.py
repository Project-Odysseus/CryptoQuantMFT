"""Performance metrics, trades and attribution from an engine result (plan 2.6).

Ported and generalised from the H14 study (the host's src/research/regime_switch.metrics and trade_records):
the same definitions, now for any result and any number of instruments.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quantlab.engine.backtest import BacktestResult


def sharpe(returns: pd.Series, periods: float = 252.0) -> float:
    """Annualised Sharpe ratio of per-bar returns (NaN without spread or with too few bars)."""
    spread = returns.std()
    return float(returns.mean() / spread * math.sqrt(periods)) if len(returns) > 20 and spread > 0 else float("nan")


def trades(held: pd.Series, net: pd.Series) -> pd.DataFrame:
    """Trades of one instrument: runs of bars holding the same sign, with their net P&L."""
    side = np.sign(held.to_numpy())
    values = net.to_numpy()
    rows, start = [], None
    for t in range(len(side) + 1):
        current = side[t] if t < len(side) else 0.0
        if start is not None and (t == len(side) or current != side[start]):
            rows.append({"entry": held.index[start], "exit": held.index[t - 1], "side": int(side[start]), "days": t - start, "pnl": float(values[start:t].sum())})
            start = None
        if start is None and t < len(side) and current != 0.0:
            start = t
    return pd.DataFrame(rows, columns=["entry", "exit", "side", "days", "pnl"])


def all_trades(result: BacktestResult) -> pd.DataFrame:
    """Every instrument's trades, with an `instrument` column."""
    frames = [trades(result.held[column], result.net[column]).assign(instrument=column) for column in result.held.columns]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["entry", "exit", "side", "days", "pnl", "instrument"])


def metrics(result: BacktestResult, start: str | pd.Timestamp | None = None, end: str | pd.Timestamp | None = None) -> dict[str, float]:
    """The standard table for a result over [start, end]: return, risk, trading and long/short figures."""
    window = result.window(start, end)
    net, gross, held = window.pnl, window.gross_pnl, window.held
    periods = result.periods
    if len(net) < 20:
        return {}
    vol = net.std() * math.sqrt(periods)
    downside = math.sqrt(float(np.mean(np.minimum(net.to_numpy(), 0.0) ** 2))) * math.sqrt(periods)
    wealth = (1.0 + net).cumprod()
    max_dd = float((wealth / wealth.cummax() - 1.0).min())
    annual = float(net.mean() * periods)
    trade_table = all_trades(window)
    wins, losses = trade_table[trade_table["pnl"] > 0]["pnl"], trade_table[trade_table["pnl"] <= 0]["pnl"]
    years = len(net) / periods
    long_pnl = window.net.where(held > 0, 0.0).sum(axis=1)
    short_pnl = window.net.where(held < 0, 0.0).sum(axis=1)
    return {"annual return": annual, "volatility": float(vol), "Sharpe": sharpe(net, periods),
            "Sortino": annual / downside if downside > 0 else float("nan"), "max drawdown": max_dd, "Calmar": annual / abs(max_dd) if max_dd < 0 else float("nan"),
            "turnover": float(window.traded.sum().sum() / years), "trades": float(len(trade_table)),
            "avg holding days": float(trade_table["days"].mean()) if len(trade_table) else float("nan"),
            "hit rate": float(len(wins) / len(trade_table)) if len(trade_table) else float("nan"),
            "avg win": float(wins.mean()) if len(wins) else float("nan"), "avg loss": float(losses.mean()) if len(losses) else float("nan"),
            "profit factor": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else float("nan"),
            "exposure": float(held.abs().sum(axis=1).mean()), "time invested": float((held.abs().sum(axis=1) > 0).mean()),
            "long contribution": float(long_pnl.sum() / years), "short contribution": float(short_pnl.sum() / years),
            "costs per year": float(window.costs.sum().sum() / years), "gross Sharpe": sharpe(gross, periods)}


def attribution(result: BacktestResult, start: str | pd.Timestamp | None = None, end: str | pd.Timestamp | None = None) -> pd.DataFrame:
    """Yearly P&L by instrument, by side, by the target's parts, and costs (fractions of equity a year)."""
    window = result.window(start, end)
    years = len(window.pnl) / result.periods
    rows: dict[str, float] = {}
    for column in window.net.columns:
        rows[f"instrument {column}"] = float(window.net[column].sum() / years)
    rows["long (net)"] = float(window.net.where(window.held > 0, 0.0).sum().sum() / years)
    rows["short (net)"] = float(window.net.where(window.held < 0, 0.0).sum().sum() / years)
    for name, frame in window.parts_pnl.items():
        rows[f"part {name} (gross)"] = float(frame.sum().sum() / years)
    rows["costs"] = -float(window.costs.sum().sum() / years)
    rows["total (net)"] = float(window.pnl.sum() / years)
    return pd.Series(rows, name="per year").to_frame()


def benchmark_stats(returns: pd.Series, benchmark: pd.Series, *, periods: float = 252.0) -> dict[str, float]:
    """Beta, annualised alpha, correlation, tracking error and information ratio against a benchmark's returns."""
    joined = pd.concat({"r": returns, "b": benchmark}, axis=1).dropna()
    if len(joined) < 20 or joined["b"].var() == 0:
        return {}
    beta = float(joined["r"].cov(joined["b"]) / joined["b"].var())
    active = joined["r"] - beta * joined["b"]
    tracking = float((joined["r"] - joined["b"]).std() * math.sqrt(periods))
    return {"beta": beta, "alpha": float(active.mean() * periods), "correlation": float(joined["r"].corr(joined["b"])),
            "tracking error": tracking, "information ratio": float((joined["r"] - joined["b"]).mean() * periods / tracking) if tracking > 0 else float("nan")}
