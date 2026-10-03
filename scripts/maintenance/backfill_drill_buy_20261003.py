"""One-off: add the live restart drill's opening buy (2026-10-03) to the trade log, tax ledger and event log.

    python scripts/maintenance/backfill_drill_buy_20261003.py          # shows what it would write
    python scripts/maintenance/backfill_drill_buy_20261003.py --write  # backs the database up, then writes

The drill's buy (0.0001 BTC at 84,619 on PF_XBTUSD) was booked in the book after the restart but skipped by the
"already logged" check, which matched the paper soak's fill with the same order id (fixed the same day). The closing
sell was recorded normally. This writes the three missing records with the engine's own functions; the fee is the
contract's taker rate, as the engine estimates it. It refuses to run twice.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import settings  # noqa: E402
from src.storage.trade_logger import TradeLogger  # noqa: E402

STAMP = datetime.fromisoformat("2026-10-03T11:24:45.599664+00:00")  # when the restarted engine booked it
ORDER_ID, BOOK_ID, PRICE, SIZE = "pf-1-0-kraken_futures-BTC/USD", "a7c85ba362", 84619.0, 0.0001
FEE = SIZE * PRICE * 0.0005
NOTE = "backfilled 2026-10-03: the restart drill's recovered fill was skipped by a dedupe bug"


def main() -> int:
    logger = TradeLogger(database_path=settings.database_path)
    if any(trade["source"] == "portfolio_live" and trade["side"] == "buy" and abs(trade["price"] - PRICE) < 1e-9 for trade in logger.list_trades(limit=200)):
        print("The buy is already in the trade log: nothing to do.")
        return 0
    print(f"Missing: buy {SIZE} BTC/USD @ {PRICE:,.0f} on kraken_futures at {STAMP:%Y-%m-%d %H:%M:%S} UTC, fee {FEE:.8f} USD (order {ORDER_ID}).")
    if "--write" not in sys.argv:
        print("Run again with --write to add the trade, its TRADING_FEE tax record and its fill event.")
        return 0
    backup = Path(f"{settings.database_path}.bak.{datetime.now(timezone.utc):%Y%m%d%H%M%S}")
    with sqlite3.connect(settings.database_path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    trade_id, error = logger.log_trade(timestamp=STAMP, source="portfolio_live", exchange="kraken_futures", pair="BTC/USD", side="buy", price=PRICE, size=SIZE, fee=FEE,
                                       strategy_id="portfolio")
    logger.log_derivative_event(timestamp=STAMP, venue_symbol="PF_XBTUSD", transaction_type="TRADING_FEE", amount=-FEE, currency="USD", source="portfolio",
                                metadata={"instrument": "kraken_futures:BTC/USD", "portfolio": "btc-live", "order_id": ORDER_ID, "note": NOTE})
    logger.log_event(timestamp=STAMP, level="INFO", event_type="portfolio_fill", message=f"buy {SIZE} kraken_futures:BTC/USD @ {PRICE} (open) [backfilled]", source="portfolio",
                     metadata={"order_id": ORDER_ID, "book_id": BOOK_ID, "instrument": "kraken_futures:BTC/USD", "side": "buy", "units": SIZE, "price": PRICE, "fee": FEE,
                               "reason": "open", "note": NOTE})
    print(f"Backup at {backup}. Wrote trade {trade_id}{' (tax note: ' + error + ')' if error else ''}, the TRADING_FEE record and the fill event.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
