"""H13, a smooth volatility-normalised trend signal on the Nasdaq-100, S&P 500 and STOXX Europe 600 (research/prereg/H13.txt).

    python scripts/research/h13_study.py            # writes data/research/h13_<time>/ and logs 21 trials
    python scripts/research/h13_study.py --no-ledger
    python scripts/research/h13_study.py --refresh  # download the index closes again
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import aiohttp
import numpy as np
import pandas as pd
from scipy.stats import skew

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import governance  # noqa: E402
from src.research import multi_asset as ma  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402
from src.research.inference import block_bootstrap_ci  # noqa: E402
from src.signals.trend_forecast import trend_forecast  # noqa: E402

HYPOTHESIS = "H13"
START, END, SPLIT = pd.Timestamp("2005-07-01"), pd.Timestamp("2025-12-31"), pd.Timestamp("2015-07-01")
CACHE = Path("data/research_cache/h13/closes.csv")
MARKETS = {  # market: (Yahoo symbol, OECD rate area, dividend yield, cost per side in bps)
    "NDX": ("^NDX", "USA", 0.009, 1.5),
    "SPX": ("^GSPC", "USA", 0.019, 1.5),
    "STOXX600": ("^STOXX", "EA20", 0.032, 3.0),
}
VARIANTS = {"P": "compound_sigmoid", "B1": "sign", "B2": "linear", "B3": "double_step", "B4": "sma_4_48"}
LABELS = {"P": "P compound sigmoid", "B1": "B1 sign", "B2": "B2 linear", "B3": "B3 double step (0.6)", "B4": "B4 4/48 average, +/-1"}
TARGET_VOL, BAND, CAP, ROLLS, ROLL_BPS = 0.10, 0.02, 1.0, 4, 1.0
HORIZONS = {"1 day": 1, "5 days": 5, "21 days": 21, "63 days": 63}


def window(frame: pd.Series | pd.DataFrame) -> pd.Series | pd.DataFrame:
    """The pre-registered window."""
    return frame[(frame.index >= START) & (frame.index <= END)]


async def _fetch_closes() -> pd.DataFrame:
    first, last = int(pd.Timestamp("1998-01-01").timestamp()), int(pd.Timestamp.now(tz="UTC").timestamp())
    async with aiohttp.ClientSession() as session:
        charts = await asyncio.gather(*(ma._get(session, ma._YAHOO.format(symbol=quote(symbol), start=first, end=last), json=True)
                                        for symbol, *_ in MARKETS.values()))
    return pd.DataFrame({market: ma.parse_yahoo_closes(chart) for market, chart in zip(MARKETS, charts)}).sort_index()


def load_closes(refresh: bool) -> pd.DataFrame:
    """Index closes from the cache, downloading once; without the frozen final holdout."""
    if refresh or not CACHE.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        asyncio.run(_fetch_closes()).to_csv(CACHE)
    closes = pd.read_csv(CACHE, index_col=0, parse_dates=True)
    return governance.trim_frame(closes, label="H13 index closes")


def build_returns(closes: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(index levels, returns above cash) on a Monday-to-Friday calendar."""
    days = pd.bdate_range(closes.index.min(), closes.index.max())
    levels = closes.reindex(closes.index.union(days)).ffill().reindex(days)
    rates = ma.daily_rates(asyncio.run(ma.load_raw(ma.load_markets()))["rates"], days)
    carry = pd.DataFrame({market: (dividend - rates[area]) / ma.DAYS_PER_YEAR for market, (_symbol, area, dividend, _cost) in MARKETS.items()})
    returns = levels.pct_change(fill_method=None) + carry
    return levels, returns.where(levels.notna() & levels.shift(1).notna())


def forecasts(variant: str, levels: pd.DataFrame, returns: pd.DataFrame) -> pd.DataFrame:
    """Each market's forecast at each day's close."""
    out = {}
    for market in MARKETS:
        series = returns[market]
        valid = series.index[levels[market].notna()]
        if VARIANTS[variant] == "sma_4_48":
            close = levels[market].loc[valid]
            difference = close.rolling(4).mean() - close.rolling(48).mean()
            values = np.sign(difference).fillna(0.0).to_numpy()
        else:
            values = trend_forecast(levels[market].loc[valid].to_numpy(), activation=VARIANTS[variant], returns=series.loc[valid].to_numpy())
        out[market] = pd.Series(values, index=valid).reindex(returns.index).fillna(0.0)
    return pd.DataFrame(out)


def volatility(returns: pd.DataFrame) -> pd.DataFrame:
    """Annualised volatility at each close, the same estimate the forecast uses."""
    from src.signals.trend_forecast import ewma_volatility

    return pd.DataFrame({market: ewma_volatility(returns[market].to_numpy()) for market in returns}, index=returns.index) * np.sqrt(ma.DAYS_PER_YEAR)


def decided_positions(forecast: pd.DataFrame, vol: pd.DataFrame) -> pd.DataFrame:
    """Positions decided at each close (share of the book's capital), after the cap and the no-trade band."""
    target = (forecast * TARGET_VOL / vol / len(MARKETS)).clip(-CAP, CAP).fillna(0.0)
    held = np.zeros(target.shape)
    current = np.zeros(target.shape[1])
    for row, values in enumerate(target.to_numpy()):
        move = np.abs(values - current) > BAND
        current = np.where(move, values, current)
        held[row] = current
    return pd.DataFrame(held, index=target.index, columns=target.columns)


def pnl(decided: pd.DataFrame, returns: pd.DataFrame, cost_multiplier: float = 1.0) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(net, gross, earning position) per market: decided at t, traded at the close of t+1, earning from t+2."""
    earning = decided.shift(2).fillna(0.0)
    gross = earning * returns.fillna(0.0)
    traded = decided.diff().abs().shift(1).fillna(0.0)  # the trade at t+1's close
    per_side = pd.Series({market: cost for market, (*_rest, cost) in MARKETS.items()}) / 10_000.0 * cost_multiplier
    roll = earning.abs() * ROLLS * ROLL_BPS / 10_000.0 / ma.DAYS_PER_YEAR * cost_multiplier
    net = gross - traded * per_side - roll
    return net, gross, earning


def trades(earning: pd.Series, daily: pd.Series) -> list[np.ndarray]:
    """Each trade's daily P&L: runs of days on which the position keeps the same non-zero sign."""
    side = np.sign(earning.to_numpy())
    values = daily.to_numpy()
    out, start = [], None
    for index in range(len(side) + 1):
        current = side[index] if index < len(side) else 0.0
        if start is not None and (index == len(side) or current != side[start]):
            out.append(values[start:index])
            start = None
        if start is None and index < len(side) and current != 0.0:
            start = index
    return out


def weighted_skew(values: np.ndarray, weights: np.ndarray) -> float:
    """Skew with each row weighted (weights sum to anything)."""
    weights = weights / weights.sum()
    mean = float(np.sum(weights * values))
    spread = float(np.sqrt(np.sum(weights * (values - mean) ** 2)))
    return float(np.sum(weights * (values - mean) ** 3)) / spread ** 3 if spread > 0 else float("nan")


def trade_stats(all_trades: list[np.ndarray]) -> dict[str, float]:
    """Closed-trade vs intra-trade skew, winners, and the average path of trades of 20 days or more."""
    finals = np.array([trade.sum() for trade in all_trades])
    days = np.concatenate(all_trades) if all_trades else np.array([])
    weights = np.concatenate([np.full(len(trade), 1.0 / len(trade)) for trade in all_trades]) if all_trades else np.array([])
    grid = np.linspace(0.0, 1.0, 21)
    long_trades = [trade for trade in all_trades if len(trade) >= 20]
    paths = np.array([np.interp(grid, np.linspace(0.0, 1.0, len(trade) + 1), np.concatenate([[0.0], np.cumsum(trade)])) for trade in long_trades])
    average = paths.mean(axis=0) if len(paths) else np.full(len(grid), np.nan)
    peak = int(np.nanargmax(average)) if len(paths) else 0
    return {"trades": len(all_trades), "winning share": float((finals > 0).mean()) if len(finals) else float("nan"),
            "closed-trade skew": float(skew(finals)) if len(finals) > 2 else float("nan"),
            "intra-trade skew (per day)": float(skew(days)) if len(days) > 2 else float("nan"),
            "intra-trade skew (per trade)": weighted_skew(days, weights) if len(days) > 2 else float("nan"),
            "trades of 20+ days": len(long_trades), "path peak at": float(grid[peak]),
            "giveback from peak": float((average[peak] - average[-1]) / average[peak]) if len(paths) and average[peak] > 0 else float("nan")}


def horizon_skew(daily: pd.Series) -> dict[str, float]:
    """Skew of non-overlapping sums of `daily` over each horizon."""
    values = daily.to_numpy()
    return {label: float(skew(values[: len(values) // days * days].reshape(-1, days).sum(axis=1))) for label, days in HORIZONS.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-ledger", action="store_true", help="do not log the trials")
    parser.add_argument("--refresh", action="store_true", help="download the index closes again")
    args = parser.parse_args()
    lock = governance.require_prereg(HYPOTHESIS)
    out = Path("data/research") / f"h13_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    governance.write_manifest(out, args=args, extra={"prereg_version": lock["version"], "prereg_sha256": lock["sha256"]})

    levels, returns = build_returns(load_closes(args.refresh))
    vol = volatility(returns)
    spx = window(returns["SPX"])
    results: dict[str, dict] = {}
    for variant in VARIANTS:
        decided = decided_positions(forecasts(variant, levels, returns), vol)
        net, gross, earning = pnl(decided, returns)
        net, gross, earning, decided_w = window(net), window(gross), window(earning), window(decided)
        results[variant] = {"net": net, "gross": gross, "earning": earning, "turnover": float(decided_w.diff().abs().sum(axis=1).mean() * ma.DAYS_PER_YEAR),
                            "trades": {market: trades(earning[market], net[market]) for market in MARKETS}}
    doubled_net = window(pnl(decided_positions(forecasts("P", levels, returns), vol), returns, cost_multiplier=2.0)[0]).sum(axis=1)

    rows, market_rows, trade_rows, skew_rows, year_rows = {}, {}, {}, {}, {}
    intervals = {}
    for variant, result in results.items():
        book, book_gross = result["net"].sum(axis=1), result["gross"].sum(axis=1)
        first, second = book[book.index < SPLIT], book[book.index >= SPLIT]
        estimate, low, high = block_bootstrap_ci(book.to_numpy(), block=60, runs=2000, seed=0, statistic=ma.sharpe)
        intervals[variant] = (estimate, low, high)
        rows[LABELS[variant]] = {**ma.summary(book), "Sharpe 95% low": low, "Sharpe 95% high": high, "Sharpe before costs": ma.sharpe(book_gross),
                                 "Sharpe 2005-07 to 2015-06": ma.sharpe(first), "Sharpe 2015-07 to 2025-12": ma.sharpe(second),
                                 "turnover x capital/yr": result["turnover"], "costs %/yr": float((book_gross.mean() - book.mean()) * ma.DAYS_PER_YEAR * 100.0),
                                 "corr with S&P 500 (monthly)": float(ma.monthly(book).corr(ma.monthly(spx)))}
        for market in MARKETS:
            series = result["net"][market]
            market_rows[(LABELS[variant], market)] = {"Sharpe": ma.sharpe(series), "Sharpe 2005-07 to 2015-06": ma.sharpe(series[series.index < SPLIT]),
                                                      "Sharpe 2015-07 to 2025-12": ma.sharpe(series[series.index >= SPLIT]), "Sortino": ma.sortino(series),
                                                      "worst drawdown": ma.max_drawdown(series), "63-day skew": horizon_skew(series)["63 days"]}
        trade_rows[LABELS[variant]] = trade_stats([trade for market in MARKETS for trade in result["trades"][market]])
        skew_rows[LABELS[variant]] = horizon_skew(book)
        yearly = (1.0 + book).groupby(book.index.year).prod() - 1.0
        year_rows[LABELS[variant]] = {str(year): float(yearly.get(year, np.nan)) * 100.0 for year in (2008, 2020, 2022)}

    table, per_market = pd.DataFrame(rows).T, pd.DataFrame(market_rows).T
    trade_table, skew_table, years = pd.DataFrame(trade_rows).T, pd.DataFrame(skew_rows).T, pd.DataFrame(year_rows).T
    primary, binary = rows[LABELS["P"]], rows[LABELS["B1"]]
    checks = {"1. P's Sharpe after costs positive over the window and in both halves": all(primary[key] > 0 for key in ("Sharpe", "Sharpe 2005-07 to 2015-06", "Sharpe 2015-07 to 2025-12")),
              "2. P's Sharpe no more than 0.10 below B1's": primary["Sharpe"] >= binary["Sharpe"] - 0.10,
              "3. P's 63-day skew higher than B1's": skew_rows[LABELS["P"]]["63 days"] > skew_rows[LABELS["B1"]]["63 days"]}
    killed = not primary["Sharpe 2015-07 to 2025-12"] > 0
    verdict = "KILL" if killed else "PASS" if all(checks.values()) else "INCONCLUSIVE"

    days = results["P"]["net"].index
    estimate, low, high = intervals["P"]
    report = [f"# H13: smooth trend activation on the Nasdaq-100, S&P 500 and STOXX Europe 600, pre-registration v{lock['version']} ({lock['sha256'][:12]})", "",
              f"{len(days)} trading days, {days[0]:%Y-%m-%d} to {days[-1]:%Y-%m-%d}; daily decisions traded the next day; returns above cash, after costs.", "",
              f"**Primary (P):** Sharpe {estimate:.2f} (95% interval {low:.2f} to {high:.2f}); 63-day skew {skew_rows[LABELS['P']]['63 days']:.2f} "
              f"vs B1 {skew_rows[LABELS['B1']]['63 days']:.2f}. At doubled costs: Sharpe {ma.sharpe(doubled_net):.2f}.", "",
              "## The book by variant (after costs)", "", md_table(table, digits=2), "",
              "## Skew of the book's returns by horizon (non-overlapping)", "", md_table(skew_table, digits=2), "",
              "## Trades (all three markets pooled)", "", md_table(trade_table, digits=2), "",
              "## Per market (after costs)", "", md_table(per_market, digits=2), "",
              "## The book in stress years, %", "", md_table(years, digits=1), "",
              "## Verdict", "", *[f"- {'pass' if ok else 'FAIL'}: {label}" for label, ok in checks.items()],
              *(["- kill rule met: P's Sharpe after costs from 2015-07 is zero or negative"] if killed else []), "", f"**{verdict}**"]
    (out / "report.md").write_text("\n".join(report))
    pd.DataFrame({variant: result["net"].sum(axis=1) for variant, result in results.items()}).to_csv(out / "daily_net.csv")
    if not args.no_ledger:
        governance.record_trials("h13_study", 21, family=HYPOTHESIS, data="^NDX, ^GSPC, ^STOXX (Yahoo), OECD rates, 2005-07 to 2025-12",
                                 details={"verdict": verdict, "sharpe_net": estimate, "interval": [low, high]})
    print("\n".join(report))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
