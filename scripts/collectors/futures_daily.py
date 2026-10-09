"""Record the daily bars of the futures contracts trading now (the free source drops a contract when it expires).

    python scripts/collectors/futures_daily.py              # once
    python scripts/collectors/futures_daily.py --every 21600   # every six hours, until Ctrl-C

Writes one CSV per contract under data/market_data/futures/daily/ (a few kilobytes each). Each run fetches the held
and the next contract of every market in config/futures_markets.csv that the source quotes, and merges the bars in.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.futures.contracts import load_markets  # noqa: E402
from src.futures.quotes import STORE_DIR, DailyStore, record  # noqa: E402
from src.futures.sessions import Sessions  # noqa: E402


async def run(every: float, directory: Path) -> None:
    """Record once, or again every `every` seconds. A failed round is printed and the loop carries on."""
    markets, store, sessions = load_markets(), DailyStore(directory), Sessions()
    while True:
        stamp = f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
        try:
            result = await record(markets, store, sessions)
            new = sum(result["added"].values())
            print(f"{stamp} {len(result['added'])} contracts, {new} new days" + (f"; failed: {result['failed']}" if result["failed"] else ""), flush=True)
        except Exception as exc:  # noqa: BLE001 - the recorder must outlive a bad round
            print(f"{stamp} round failed: {type(exc).__name__}: {exc}", flush=True)
        if every <= 0:
            return
        await asyncio.sleep(every)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--every", type=float, default=0.0, help="seconds between rounds; 0 records once")
    parser.add_argument("--dir", type=Path, default=STORE_DIR, help="where the CSVs go")
    args = parser.parse_args()
    asyncio.run(run(args.every, args.dir))


if __name__ == "__main__":
    main()
