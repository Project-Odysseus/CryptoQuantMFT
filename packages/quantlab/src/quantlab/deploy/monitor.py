"""Is the paper or live book doing what research said it would? Parity and expectation monitoring (plan 7.3-7.4).

**Parity** (`parity`): the book's positions and P&L against the research engine replayed on the same days. Positions
are compared per instrument (sign agreement, size ratio, correlation) and P&L day by day (tracking error, the
cumulative gap). A divergence is flagged with its likely cause:

- signs disagree on more than `sign_tolerance` of days: timing or data (a different bar, a missed update);
- sizes differ by a stable ratio outside `size_band`: sizing (volatility estimate, scale, allocation);
- P&L tracking error above `tracking_tolerance` a year with positions in line: prices, fills or costs.

**Expectation** (`expectation_check`, generalising the host's `src/portfolio/review.py`): live results against the
spec's expectations, each with a status (ok / watch / flag) that never acts on its own:

- Sharpe: live minus the shrunk Sharpe research expects, in standard errors (Lo, 2002); "flag" below -2 once
  `min_days` exist ("decay"), and a live Sharpe below 0 after `floor_days`;
- drawdown: beyond the backtest's worst ("watch"), beyond `drawdown_multiple` times it ("flag");
- volatility, turnover, costs: outside [0.5, 1.5] x what research expects ("watch"), [0.33, 3] ("flag").

These read data inside the frozen holdout (paper and live run there), through `operational_lab`: an explicit,
logged opening for monitoring rules already fixed, never for research.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def operational_lab(spec: Any, *, start: str | None = None, reason: str = "operational monitoring", **lab_options: Any) -> Any:
    """A Lab on the spec's universe with the frozen holdout open, for parity and expectation monitoring (logged)."""
    from quantlab.data.catalog import DataCatalog
    from quantlab.deploy.spec import spec_lab
    from quantlab.validation import ledger

    catalog = lab_options.pop("catalog", None) or DataCatalog()
    catalog.unlock(f"{spec.key}: {reason}")
    ledger.append({"type": "operational_access", "spec": spec.key, "reason": reason})
    with _monitoring():
        return spec_lab(spec, start=start, catalog=catalog, **lab_options)


class _monitoring:
    """Lets holdout_cut pass data through while the operational lab loads (the catalog logs every unlocked read)."""

    def __enter__(self) -> None:
        from quantlab.validation import ledger

        self._before, ledger._open_look = ledger._open_look, "operational monitoring"

    def __exit__(self, *exc: Any) -> None:
        from quantlab.validation import ledger

        ledger._open_look = self._before


def parity(live_weights: pd.DataFrame, research_weights: pd.DataFrame, *, live_pnl: pd.Series | None = None, research_pnl: pd.Series | None = None,
           sign_tolerance: float = 0.05, size_band: tuple[float, float] = (0.8, 1.25), tracking_tolerance: float = 0.02) -> dict[str, Any]:
    """Positions and P&L of the book against the research replay (see the module docstring)."""
    columns = [column for column in live_weights.columns if column in research_weights.columns]
    index = live_weights.index.intersection(research_weights.index)
    live, research = live_weights.loc[index, columns].astype(float), research_weights.loc[index, columns].astype(float)
    rows = {}
    for column in columns:
        a, b = live[column], research[column]
        both = (a.abs() > 1e-9) | (b.abs() > 1e-9)
        agree = float((np.sign(a[both]) == np.sign(b[both])).mean()) if both.any() else 1.0
        held = (a.abs() > 1e-9) & (b.abs() > 1e-9)
        ratio = float((a[held] / b[held]).abs().median()) if held.any() else float("nan")
        flags = []
        if 1.0 - agree > sign_tolerance:
            flags.append("signs disagree: timing or data")
        if np.isfinite(ratio) and not size_band[0] <= ratio <= size_band[1]:
            flags.append(f"size ratio {ratio:.2f}: sizing")
        rows[column] = {"days": int(len(a)), "sign agreement": agree, "size ratio (live / research)": ratio,
                        "correlation": float(a.corr(b)) if a.std() > 0 and b.std() > 0 else float("nan"),
                        "mean |difference|": float((a - b).abs().mean()), "flags": "; ".join(flags) or "ok"}
    table = pd.DataFrame(rows).T
    out: dict[str, Any] = {"positions": table, "ok": bool((table["flags"] == "ok").all()) if len(table) else True}
    if live_pnl is not None and research_pnl is not None:
        joined = pd.concat({"live": live_pnl, "research": research_pnl}, axis=1).dropna()
        difference = joined["live"] - joined["research"]
        tracking = float(difference.std() * math.sqrt(252.0)) if len(difference) > 1 else float("nan")
        pnl = {"days": len(joined), "tracking error (annual)": tracking, "cumulative gap": float(difference.sum()),
               "correlation": float(joined["live"].corr(joined["research"])) if len(joined) > 2 else float("nan")}
        pnl["flag"] = "P&L diverges: prices, fills or costs" if np.isfinite(tracking) and tracking > tracking_tolerance else "ok"
        out["pnl"] = pd.Series(pnl, name="value")
        out["ok"] = out["ok"] and pnl["flag"] == "ok"
        out["daily difference"] = difference
    return out


def sharpe_error(sharpe: float, days: int, periods: float = 252.0) -> float:
    """Standard error of an annual Sharpe from `days` daily returns (Lo, 2002)."""
    daily = sharpe / math.sqrt(periods)
    return math.sqrt((1.0 + daily ** 2 / 2.0) / max(days, 1)) * math.sqrt(periods)


def _band(value: float, expected: float | None, watch: tuple[float, float] = (0.5, 1.5), flag: tuple[float, float] = (1 / 3, 3.0)) -> str:
    if expected is None or not np.isfinite(value) or expected == 0:
        return "-"
    ratio = value / expected
    if not flag[0] <= ratio <= flag[1]:
        return "flag"
    return "watch" if not watch[0] <= ratio <= watch[1] else "ok"


def expectation_check(pnl: pd.Series, expectations: Any, *, turnover: float | None = None, costs_per_year: float | None = None, min_days: int = 90,
                      floor_days: int = 365, decay_z: float = 2.0, drawdown_multiple: float = 1.5, periods: float = 252.0) -> pd.DataFrame:
    """Live (or paper) results against the spec's expectations (see the module docstring)."""
    pnl = pnl.dropna()
    days = len(pnl)
    spread = pnl.std()
    sharpe = float(pnl.mean() / spread * math.sqrt(periods)) if days > 2 and spread > 0 else float("nan")
    growth = (1.0 + pnl).cumprod()
    drawdown = float((growth / growth.cummax() - 1.0).min()) if days else float("nan")
    volatility = float(spread * math.sqrt(periods)) if days > 2 else float("nan")
    expected = expectations.shrunk_sharpe if expectations.shrunk_sharpe is not None else expectations.sharpe
    rows: dict[str, dict[str, Any]] = {}
    if expected is not None and np.isfinite(sharpe):
        z = (sharpe - expected) / sharpe_error(expected, days, periods)
        status = "too early" if days < min_days else "flag" if z < -decay_z else "watch" if z < -decay_z / 2 else "ok"
        if days >= floor_days and sharpe < 0:
            status = "flag"
        rows["Sharpe"] = {"live": sharpe, "expected": expected, "detail": f"{z:+.1f} standard errors over {days} days", "status": status}
    if expectations.max_drawdown is not None and np.isfinite(drawdown):
        worst = abs(expectations.max_drawdown)
        status = "flag" if abs(drawdown) > drawdown_multiple * worst else "watch" if abs(drawdown) > worst else "ok"
        rows["drawdown"] = {"live": drawdown, "expected": expectations.max_drawdown, "detail": f"flag beyond {drawdown_multiple:g} x the backtest's worst", "status": status}
    for name, value, wanted in (("volatility", volatility, expectations.volatility), ("turnover", turnover, expectations.turnover),
                                ("costs a year", costs_per_year, expectations.costs_per_year)):
        if value is not None and wanted is not None:
            rows[name] = {"live": value, "expected": wanted, "detail": "watch outside 0.5-1.5x, flag outside 0.33-3x", "status": "too early" if days < min_days else _band(value, wanted)}
    table = pd.DataFrame(rows).T
    if len(table):
        table.attrs["overall"] = "flag" if (table["status"] == "flag").any() else "watch" if (table["status"] == "watch").any() else "ok"
    return table
