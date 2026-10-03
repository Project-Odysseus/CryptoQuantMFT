"""Does each side of the taker-buy basket earn its place? The long coins and the short coins, each against the universe.

    python scripts/research/basket_legs.py [config/portfolio.taker_paper.toml]

A long/short basket bets that its long coins beat its short coins. That bet can pay because the longs beat the
market, because the shorts lag it, or both, and the combined result doesn't say which. This takes the basket exactly
as configured (no parameter is changed) and reports, on Kraken perp prices:

- the return of the coins held long, of the coins held short, and of the whole ranked universe (equal weight, held
  between the same rebalance days);
- each side's excess over the universe: longs minus universe, and universe minus shorts (positive = the shorted
  coins did lag);
- what each side paid in funding (Binance's settled rates per coin, as a proxy for Kraken's) and in trading costs
  (Kraken's taker fee plus each instrument's configured slippage).

In-sample to 2024-09-30, holdout 2024-10-01 on; the frozen final holdout (2026) stays locked. A diagnosis of one
existing configuration: logged as 1 look (family "cross_sectional").
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.binance_archive import load_panel  # noqa: E402
from src.portfolio.backtest import default_bar_loader  # noqa: E402
from src.portfolio.basket import PANEL_FIELDS, SIGNALS, binance_panel, binance_symbol_map, is_rebalance_day  # noqa: E402
from src.portfolio.config import load_portfolio_config  # noqa: E402
from src.research.governance import record_trials, write_manifest  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402
from src.research.portfolio import liquid_universe, rank_weights  # noqa: E402

HOLDOUT = pd.Timestamp("2024-10-01", tz="UTC")
DAYS = 365.0


def _held(frame: pd.DataFrame, rebalance: pd.Series) -> pd.DataFrame:
    """Values set on rebalance days and held until the next one, stamped at the decision time (the next midnight)."""
    held = frame.where(rebalance, np.nan).ffill().fillna(0.0)
    held.index = held.index + pd.Timedelta(days=1)
    return held


def leg_series(config_path: str) -> tuple[pd.DataFrame, dict[str, float]]:
    """Daily series per side: coin returns, funding paid and costs, each for 1x of equity in that side."""
    config = load_portfolio_config(config_path)
    [spec] = config.baskets
    panel = binance_panel(spec.coins)
    coins = [coin for coin in spec.coins if coin in panel["close"].columns]
    frames = {name: panel[name].reindex(columns=coins) for name in PANEL_FIELDS}

    closes = {}
    for coin in coins:
        bars = default_bar_loader(config.instruments[spec.instrument(coin)], "1d")
        series = pd.Series([float(bar.close) for bar in bars], index=pd.DatetimeIndex([bar.timestamp for bar in bars]).floor("D"))
        closes[coin] = series[~series.index.duplicated(keep="last")]
    close = pd.DataFrame(closes).sort_index()
    close.index = pd.DatetimeIndex(close.index).tz_convert("UTC") if close.index.tz is not None else pd.DatetimeIndex(close.index).tz_localize("UTC")
    tradable = close.notna()

    score = SIGNALS[spec.signal](frames)
    universe = liquid_universe(frames["quote_volume"], top_n=spec.top_n) & tradable.reindex(index=score.index, columns=coins).fillna(False).astype(bool)
    raw = rank_weights(score, universe, quantile=spec.quantile, gross=spec.gross, min_names=spec.min_names)
    rebalance = pd.Series([is_rebalance_day(day + pd.Timedelta(days=1), spec.rebalance_days) for day in raw.index], index=raw.index)
    weights = _held(raw, rebalance)  # the basket's own weights: +gross/2 spread over the longs, -gross/2 over the shorts
    members = _held(universe.astype(float).where(raw.abs().sum(axis=1) > 0, 0.0), rebalance)  # the ranked universe on the same days

    returns = close.pct_change(fill_method=None).reindex(index=weights.index, columns=coins)  # bar stamped T: held from the decision at T
    funding_raw = load_panel("funding")
    mapping = binance_symbol_map(coins, funding_raw["symbol"].unique())
    funding = funding_raw[funding_raw["symbol"].isin(mapping.values())].assign(coin=lambda frame: frame["symbol"].map({v: k for k, v in mapping.items()}))
    funding = funding.pivot_table(index="date", columns="coin", values="funding").reindex(index=weights.index, columns=coins).fillna(0.0)
    cost_rate = pd.Series({coin: config.instruments[spec.instrument(coin)].taker_fee_pct / 100.0 + config.instruments[spec.instrument(coin)].slippage_bps / 10_000.0 for coin in coins})

    def side(mask_weights: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
        """(coin return, funding a long holder of these coins pays, cost of this side's trades), per 1x held in the side."""
        total = mask_weights.sum(axis=1)
        normal = mask_weights.div(total.replace(0.0, np.nan), axis=0).fillna(0.0)
        traded = normal.diff().abs().fillna(normal.abs())
        return (normal * returns.fillna(0.0)).sum(axis=1), (normal * funding).sum(axis=1), (traded * cost_rate).sum(axis=1)

    long_ret, long_funding, long_cost = side(weights.clip(lower=0.0))
    short_ret, short_funding, short_cost = side((-weights).clip(lower=0.0))
    universe_ret, _universe_funding, _universe_cost = side(members)
    active = weights.abs().sum(axis=1) > 0
    table = pd.DataFrame({
        "universe": universe_ret, "long_coins": long_ret, "short_coins": short_ret,
        "long_excess": long_ret - universe_ret, "short_excess": universe_ret - short_ret,  # both positive when that side helps
        "long_funding_paid": long_funding, "short_funding_received": short_funding, "long_cost": long_cost, "short_cost": short_cost,
        "names_long": (weights > 0).sum(axis=1), "names_short": (weights < 0).sum(axis=1),
    })[active]
    facts = {"coins": len(coins), "first": table.index[0], "last": table.index[-1], "names_long": float(table["names_long"].median()), "names_short": float(table["names_short"].median())}
    return table, facts


def summarise(table: pd.DataFrame) -> pd.DataFrame:
    """Annualised % per period and year, with a t-statistic for each side's excess (daily, independent-day approximation)."""
    def row(part: pd.DataFrame) -> dict[str, float]:
        def t_stat(values: pd.Series) -> float:
            return float(values.mean() / values.std(ddof=1) * np.sqrt(len(values))) if len(values) > 2 and values.std(ddof=1) > 0 else float("nan")

        annual = lambda name: float(part[name].mean() * DAYS * 100.0)  # noqa: E731
        long_net = annual("long_excess") - annual("long_funding_paid") - annual("long_cost")
        short_net = annual("short_excess") + annual("short_funding_received") - annual("short_cost")
        return {"days": float(len(part)), "universe": annual("universe"), "long coins": annual("long_coins"), "short coins": annual("short_coins"),
                "long excess": annual("long_excess"), "t": t_stat(part["long_excess"]), "short excess": annual("short_excess"), "t ": t_stat(part["short_excess"]),
                "long funding paid": annual("long_funding_paid"), "short funding recv": annual("short_funding_received"),
                "long cost": annual("long_cost"), "short cost": annual("short_cost"), "long side net": long_net, "short side net": short_net}

    rows = {"in-sample": row(table[table.index < HOLDOUT]), "holdout": row(table[table.index >= HOLDOUT]), "all": row(table)}
    for year, part in table.groupby(table.index.year):
        if len(part) >= 30:  # a stub of a few days at the edge of the data says nothing
            rows[str(year)] = row(part)
    return pd.DataFrame(rows).T


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", nargs="?", default="config/portfolio.taker_paper.toml")
    parser.add_argument("--no-ledger", action="store_true")
    args = parser.parse_args()
    table, facts = leg_series(args.config)
    summary = summarise(table)
    out = Path("data/research") / f"basket_legs_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    write_manifest(out, args=args)
    table.to_csv(out / "daily.csv")
    summary.to_csv(out / "summary.csv")
    if not args.no_ledger:
        record_trials("basket_legs", 1, family="cross_sectional", data="kraken perps (daily) to 2025-12-31", details={"config": args.config, "note": "leg decomposition of the existing basket, no parameter changed"})
    report = [f"# The taker-buy basket, side by side ({facts['first']:%Y-%m-%d} to {facts['last']:%Y-%m-%d}; {facts['coins']} coins, about {facts['names_long']:.0f} long and {facts['names_short']:.0f} short)", "",
              "Annualised % for 1x of equity held in that side. Excess is against the equal-weight ranked universe: long excess = long coins - universe; "
              "short excess = universe - short coins (positive when the shorted coins lagged). Net = excess, minus costs, minus funding paid (long) or plus funding received (short).", "",
              md_table(summary, digits=1), ""]
    (out / "report.md").write_text("\n".join(report))
    print("\n".join(report))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
