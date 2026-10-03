"""Record Kraken Futures spreads and book depth for a portfolio config's perps (public data, no keys).

    python scripts/collectors/kraken_spreads.py config/portfolio.multi_paper.toml          # until Ctrl-C
    python scripts/collectors/kraken_spreads.py config/portfolio.multi_paper.toml --once   # one sample of each, then exit

Every `--ticker-seconds` (10 minutes) it stores every perp's best bid and ask from one request; every
`--depth-seconds` (2 hours) it stores the config's coins' order-book cost at 100, 1,000 and 5,000 USD. About 7 MB of
network and well under 1 MB of disk a day. Files: data/market_data/kraken_spreads/{tickers,depth}/<day>.csv.
`python scripts/research/kraken_costs.py <config>` compares what was recorded with the config's assumed slippage.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data import kraken_spreads  # noqa: E402
from src.data.kraken_futures import venue_symbol_for  # noqa: E402
from src.portfolio.config import load_portfolio_config  # noqa: E402


def config_symbols(path: str) -> list[str]:
    """The venue symbols (PF_XBTUSD ...) of a config's Kraken Futures perps."""
    config = load_portfolio_config(path)
    return sorted({venue_symbol_for(spec.symbol) for spec in config.instruments.values() if spec.venue == "kraken_futures" and spec.kind == "perp"})


async def run(symbols: list[str], *, ticker_seconds: float, depth_seconds: float, once: bool) -> None:
    last_depth = 0.0
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        try:
            rows = await asyncio.to_thread(kraken_spreads.record_tickers)  # blocking HTTP stays off the event loop
            print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} tickers: {rows} perps", flush=True)
        except Exception as exc:  # noqa: BLE001 - a failed sample is a gap, not a reason to stop
            print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} tickers failed: {type(exc).__name__}: {exc}", flush=True)
        if once or started - last_depth >= depth_seconds or last_depth == 0.0:
            rows = await asyncio.to_thread(kraken_spreads.record_depth, symbols)
            last_depth = started
            print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} depth: {rows} rows for {len(symbols)} coins", flush=True)
        if once:
            return
        await asyncio.sleep(max(1.0, ticker_seconds - (loop.time() - started)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", help="Portfolio TOML whose perps get depth samples")
    parser.add_argument("--ticker-seconds", type=float, default=600.0)
    parser.add_argument("--depth-seconds", type=float, default=7200.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(config_symbols(args.config), ticker_seconds=args.ticker_seconds, depth_seconds=args.depth_seconds, once=args.once))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
