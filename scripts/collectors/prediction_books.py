"""Record prediction-market quotes and order books for BTC and ETH price markets (Kalshi, Polymarket; public data).

    python scripts/collectors/prediction_books.py                 # until Ctrl-C, one sample every 10 minutes
    python scripts/collectors/prediction_books.py --once
    python scripts/collectors/prediction_books.py --interval 120  # on wifi: every 2 minutes

Files under data/market_data/prediction_markets/ (see src/data/prediction_recorder.py). At the default interval it
uses about 10 MB of network a day; the short markets (5 and 15 minutes) are only sampled, not followed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.prediction_recorder import PredictionRecorder  # noqa: E402


async def run(recorder: PredictionRecorder, *, interval: float, once: bool) -> None:
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        report = await asyncio.to_thread(recorder.cycle)  # blocking HTTP stays off the event loop
        errors = f"; {len(report['errors'])} error(s): {report['errors'][0]}" if report["errors"] else ""
        print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} quotes {report['quotes']}, depth rows {report['depth']}, new results {report['results']}{errors}", flush=True)
        if once:
            return
        await asyncio.sleep(max(1.0, interval - (loop.time() - started)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=600.0, help="Seconds between samples (default 600)")
    parser.add_argument("--coins", nargs="*", default=["BTC", "ETH"])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(PredictionRecorder(coins=args.coins), interval=args.interval, once=args.once))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
