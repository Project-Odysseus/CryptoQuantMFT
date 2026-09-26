"""Funding carry on BTC and ETH (long spot, short perp): what does it earn after fees, per venue?

Funding histories: Binance (from 2019-09), Bybit (2020-03), Deribit (2019-06),
from the positioning cache (src/data/positioning.py, small top-ups only), and
Kraken's last year from its API. Fees are each venue's entry tier, taker, both
legs, in and out, plus 2 bps slippage per leg. These are assumptions until the
account tiers are read with API keys (TODO.MD). The Deribit and Binance/Bybit
spot legs assume a 0.10% spot venue. Kraken's is Kraken spot at 0.40%.

Two pre-specified rules:
- always on: hold the carry the whole time, paying one round trip;
- funding filter: open when the 7-day mean funding is above 10% a year, close
  when it drops below 0.

Not included: the basis at entry and exit (the perp's premium over spot), and
moving collateral between the legs. `net_on_capital` assumes 2x leverage on
the perp, i.e. 1.5 units of capital per unit of notional.

Usage:
    python scripts/research/carry_study.py
    python scripts/research/carry_study.py --enter-above 0.05 --no-kraken
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from src.data.positioning import load_series
from src.research.carry import carry_backtest, daily_funding

VENUE_FEES_PCT = {  # (spot leg, perp leg), taker, entry tier
    "binance": (0.10, 0.05),
    "bybit": (0.10, 0.055),
    "deribit": (0.10, 0.05),
    "kraken": (0.40, 0.05),
}
SLIPPAGE_BPS = 2.0


def load_daily_funding(coin: str, *, kraken: bool) -> dict[str, pd.Series]:
    """Daily funding per unit of long notional, per venue."""
    series = {}
    for venue, name, scale in (("binance", "binance_funding", 1.0), ("bybit", "bybit_funding", 1.0), ("deribit", "deribit_funding", 1 / 8)):
        frame = load_series(name, coin)
        series[venue] = daily_funding(frame["timestamp"], frame[name], payments_per_value=scale)
    if kraken:
        from src.data.kraken_futures import fetch_funding_history, venue_symbol_for

        try:
            rates = fetch_funding_history(venue_symbol_for(f"{coin}/USD"))
            series["kraken"] = daily_funding([rate.timestamp for rate in rates], [rate.hourly_rate for rate in rates])
        except Exception as exc:  # noqa: BLE001 - Kraken is optional here
            print(f"Kraken funding unavailable for {coin}: {exc!r}")
    today = pd.Timestamp.now(tz="UTC").floor("D")
    return {venue: values[values.index < today] for venue, values in series.items()}  # today is incomplete


def round_trip_cost(venue: str) -> float:
    """Both legs in and out, fees plus slippage, as a fraction of notional."""
    spot, perp = VENUE_FEES_PCT[venue]
    return 2 * (spot + perp) / 100 + 4 * SLIPPAGE_BPS / 10_000


def main() -> None:
    """Print funding levels by year and the net carry per venue, full history and the last 12 months."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH"])
    parser.add_argument("--enter-above", type=float, default=0.10, help="Open when 7-day mean funding, annualised, is above this")
    parser.add_argument("--exit-below", type=float, default=0.0, help="Close when it falls below this")
    parser.add_argument("--no-kraken", action="store_true", help="Skip Kraken's funding (an API call)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    out = Path(args.out) if args.out else Path("data/research") / f"carry_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    out.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 220)

    level_rows, rows = [], []
    rules = {"always on": None, f"filter >{args.enter_above:.0%} / <{args.exit_below:.0%}": args.enter_above}
    for coin in args.coins:
        funding = load_daily_funding(coin, kraken=not args.no_kraken)
        last_year_start = min(values.index.max() for values in funding.values()) - pd.Timedelta(days=364)
        for venue, values in funding.items():
            yearly = values.groupby(values.index.year).mean() * 365 * 100
            level_rows.append({"coin": coin, "venue": venue, "from": f"{values.index.min():%Y-%m}", **{str(year): value for year, value in yearly.items()},
                               "all": values.mean() * 365 * 100, "negative_days": (values < 0).mean()})
            windows = {"full history": values, "last 12 months": values[values.index >= last_year_start]}
            for window, data in windows.items():
                if venue == "kraken" and window == "full history":
                    continue  # Kraken only publishes the last year
                for rule, enter in rules.items():
                    result = carry_backtest(data, round_trip_cost=round_trip_cost(venue), enter_above=enter, exit_below=args.exit_below)
                    rows.append({"coin": coin, "venue": venue, "window": window, "rule": rule, "from": f"{data.index.min():%Y-%m}", **result.summary(capital_per_notional=1.5)})
                    if window == "full history" and venue == "binance":
                        by_year = result.net.groupby(result.net.index.year).sum() * 100
                        level_rows[-1].update({f"net {rule} {year}": value for year, value in by_year.items()})

    levels = pd.DataFrame(level_rows)
    table = pd.DataFrame(rows)
    levels.to_csv(out / "funding_levels.csv", index=False)
    table.to_csv(out / "carry.csv", index=False)
    year_columns = [column for column in levels.columns if column.isdigit()]
    print("Mean funding, % a year (positive = shorts receive), and share of negative days:")
    print(levels[["coin", "venue", "from", *year_columns, "all", "negative_days"]].round(2).to_string(index=False))
    for window in ("full history", "last 12 months"):
        print(f"\nCarry after fees, {window} (% of notional a year; on capital = at 2x perp leverage):")
        view = table[table.window == window][["coin", "venue", "rule", "from", "funding_pct_per_year", "cost_pct_per_year", "net_pct_per_year", "net_on_capital_pct_per_year", "time_in_position", "entries_per_year", "worst_30d_pct"]]
        print(view.round(2).to_string(index=False))
    print(f"\nRound trip per venue (both legs, in and out): " + ", ".join(f"{venue} {round_trip_cost(venue):.2%}" for venue in VENUE_FEES_PCT))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
