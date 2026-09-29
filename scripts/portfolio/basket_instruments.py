"""Write a basket portfolio config from Kraken Futures' public instrument list (no keys, no orders).

    python scripts/portfolio/basket_instruments.py --count 40 --out config/portfolio.taker_paper.toml

Picks the `count` most-traded Kraken Futures linear perps (PF_*) whose coin also has a Binance USDT perp (the basket's
signal comes from Binance's candles), leaves out non-crypto contracts (tokenized gold, oil, equities), and writes one
[instruments] table per coin with Kraken's own lot size and a slippage tier from its 24h volume, plus the basket
block. Re-run it to refresh the list; review the diff before using it, since the universe is part of the strategy.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

NOT_CRYPTO = {"XAUT", "PAXG", "WTIOIL", "BRENTOIL", "XAU", "XAG", "SPX", "NDX", "TSLA", "NVDA", "AAPL", "MSTR", "COIN"}
SLIPPAGE_TIERS = ((5e7, 5.0), (1e7, 10.0), (2e6, 20.0))  # 24h USD volume -> bps; thinner than the last tier: 30


def slippage_bps(volume_usd: float) -> float:
    """Assumed slippage per side from Kraken's 24h volume (conservative; research stress-tests 2-3x costs)."""
    return next((bps for minimum, bps in SLIPPAGE_TIERS if volume_usd >= minimum), 30.0)


def candidates(count: int) -> list[dict[str, float | str]]:
    """The most-traded Kraken PF perps with a Binance counterpart, most liquid first."""
    from src.data.binance_archive import CACHE_DIR, base_asset
    from src.data.kraken_futures import fetch_instruments, fetch_tickers

    binance = {base_asset(path.stem) for path in (CACHE_DIR / "klines_1d").glob("*USDT.parquet")}
    tickers = fetch_tickers()
    rows = []
    for instrument in fetch_instruments():
        symbol = str(instrument.get("symbol", ""))
        if not (symbol.startswith("PF_") and symbol.endswith("USD")) or not instrument.get("tradeable", True):
            continue
        coin = "BTC" if symbol[3:-3] == "XBT" else symbol[3:-3]
        if coin in NOT_CRYPTO or coin not in binance:
            continue
        ticker = tickers.get(symbol, {})
        step = 10 ** (-float(instrument.get("contractValueTradePrecision", 0)))
        rows.append({"coin": coin, "symbol": symbol, "step": step, "volume": float(ticker.get("volumeQuote") or 0.0), "mark": float(ticker.get("markPrice") or 0.0)})
    return sorted(rows, key=lambda row: -float(row["volume"]))[:count]


def render(rows: list[dict[str, float | str]], *, name: str, equity: float) -> str:
    """The config text."""
    coins = [str(row["coin"]) for row in rows]
    lines = [
        f"# Cross-sectional taker-buy basket, PAPER: {len(rows)} Kraken Futures perps, generated {datetime.now(timezone.utc):%Y-%m-%d} by",
        "# scripts/portfolio/basket_instruments.py from Kraken's public instrument list (the most-traded PF perps with a Binance perp).",
        "# Research: docs/research_log.md 2026-09-26 (\"Diversifying the trend book\") and 2026-09-29 (on Kraken prices).",
        "# Not for live at small capital: one lot of the largest-minimum coin needs a few hundred USD of equity per coin weight.",
        "# Check:  python main.py --portfolio-check config/portfolio.taker_paper.toml",
        "# Paper:  python main.py --runtime paper --portfolio config/portfolio.taker_paper.toml --runtime-iterations 0 --runtime-interval 300",
        "",
        "[portfolio]",
        f'name = "{name}"',
        'base_currency = "USD"',
        f"initial_equity = {equity:g}             # paper equity: large enough that every coin's weight is at least a few lots",
        "rebalance_band = 0.01",
        'allocation = "equal"',
        "",
        "[risk]",
        "max_gross_exposure = 1.5",
        "max_net_exposure = 0.5              # the basket is dollar-neutral by construction",
        "max_instrument_weight = 0.25",
        "max_drawdown = 0.40",
        "daily_loss_limit = 0.10",
        "stale_after_bars = 2",
        "",
        "[[baskets]]",
        'id = "taker"',
        'signal = "taker_buy_share_7d"',
        "top_n = 30                          # each day: the 30 most-traded of the coins below (Binance 30-day volume)",
        "quantile = 0.2                      # long the top fifth by taker-buy share, short the bottom fifth",
        "gross = 1.0",
        "min_names = 12",
        "rebalance_days = 10                 # the slow signal: every 10 days survived 3x costs in research",
        f"coins = {coins}".replace("'", '"'),
        "",
    ]
    for row in rows:
        lines += [f'[instruments."kraken_futures:{row["coin"]}/USD"]', 'kind = "perp"', "max_leverage = 2.0",
                  f"lot_step = {row['step']:g}", f"min_order_size = {row['step']:g}",
                  f"slippage_bps = {slippage_bps(float(row['volume'])):g}              # 24h volume {float(row['volume']):,.0f} USD", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=40, help="Candidate coins (the basket picks its top_n from them each day)")
    parser.add_argument("--name", default="taker-paper")
    parser.add_argument("--equity", type=float, default=10_000.0, help="Paper starting equity (USD)")
    parser.add_argument("--out", type=Path, default=None, help="Write here instead of printing")
    args = parser.parse_args()
    text = render(candidates(args.count), name=args.name, equity=args.equity)
    if args.out:
        args.out.write_text(text)
        print(f"Wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
