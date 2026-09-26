"""The kill switch's exchange sweep: make the Kraken accounts safe on their own, with no runtime running.

The kill switch's state file stops every runtime that reads it, but a runtime
that is down (a crashed machine, a closed laptop) reads nothing, and its
orders and positions stay live on the exchange. That is when a kill switch is
needed most. So `python main.py --kill-switch` also acts on the exchanges
directly:

- **Kraken spot:** every open order is recovered from Kraken and cancelled,
  then `CancelAll` sweeps anything left. Coins are not sold: they aren't
  leveraged, and selling is a choice (and a taxable event).
- **Kraken Futures:** every open order is cancelled, and every position is
  closed with a reduce-only market order. Reduce-only means a close can never
  open or flip a position, so it is safe even if a running runtime closes the
  same position at the same moment. The fills are recorded in the trade log
  and the tax ledger, as the portfolio's own closes are.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.data.kraken_futures import PERP_VENUE_SYMBOLS

FUTURES_TAKER_FEE_RATE = 0.0005  # Kraken Futures' entry-tier taker fee; /fills reports no fee, so it is estimated
FLAT_TIMEOUT_SECONDS = 30.0
_RUNTIME_SYMBOLS = {venue: symbol for symbol, venue in PERP_VENUE_SYMBOLS.items()}


def recover_spot_open_orders(adapter: Any) -> int:
    """Load Kraken's open spot orders into the adapter, so the kill switch can cancel them by id. Returns how many."""
    open_orders = adapter.fetch_open_orders()
    if open_orders:
        adapter.recover_execution_state(remote_orders=open_orders)
    return len(open_orders)


def _format_size(size: float) -> str:
    return format(Decimal(str(size)).normalize(), "f")


def flatten_kraken_futures(
    client: Any,
    *,
    trade_logger: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = FLAT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Cancel every open Kraken Futures order, close every position reduce-only, and wait until the account is flat.

    Args:
        client: A `KrakenFuturesPrivateClient` (anything with its `call`).
        trade_logger: Records each close as a trade, and its realized P&L and
            estimated fee in the tax ledger.
        timeout: Seconds to wait for the positions to close and the fills to
            appear before reporting what is still open.

    Returns:
        The cancel result, one entry per close (side, size, entry, fill price,
        realized P&L, fee), and the positions and orders still open at the
        end: both empty means the account is safe.
    """
    stamp = datetime.now(timezone.utc)
    cancelled = dict(client.call("POST", "cancelallorders", {}).get("cancelStatus") or {})
    closes: list[dict[str, Any]] = []
    for index, position in enumerate(client.call("GET", "openpositions").get("openPositions", [])):
        size = abs(float(position["size"]))
        if size <= 0.0:
            continue
        long = position.get("side") == "long"
        close = {"venue_symbol": position["symbol"], "side": "sell" if long else "buy", "size": size, "entry_price": float(position["price"]),
                 "direction": 1.0 if long else -1.0, "cli_ord_id": f"cqm-kill-{stamp:%Y%m%dT%H%M%S}-{index}", "filled": 0.0}
        try:
            payload = client.call("POST", "sendorder", {"orderType": "mkt", "symbol": close["venue_symbol"], "side": close["side"],
                                                        "size": _format_size(size), "cliOrdId": close["cli_ord_id"], "reduceOnly": "true"})
            close["send_status"] = str((payload.get("sendStatus") or {}).get("status", ""))
        except Exception as exc:  # noqa: BLE001 - one failed close must not stop the others
            close["send_status"] = f"error: {exc}"
        closes.append(close)

    waited = 0.0
    remaining: list[dict[str, Any]] = []
    while True:
        remaining = [position for position in client.call("GET", "openpositions").get("openPositions", []) if abs(float(position.get("size", 0.0))) > 0.0]
        fills = client.call("GET", "fills").get("fills", []) if closes else []
        for close in closes:
            own = [fill for fill in fills if fill.get("cliOrdId") == close["cli_ord_id"]]
            close["filled"] = sum(float(fill["size"]) for fill in own)
            if close["filled"] > 0.0:
                close["fill_price"] = sum(float(fill["size"]) * float(fill["price"]) for fill in own) / close["filled"]
        if (not remaining and all(close["filled"] > 0.0 for close in closes if close["send_status"] in {"placed", "filled", "partiallyFilled"})) or waited >= timeout:
            break
        sleep(1.0)
        waited += 1.0

    for close in closes:
        if close["filled"] <= 0.0:
            continue
        close["realized_pnl"] = close["direction"] * (close["fill_price"] - close["entry_price"]) * close["filled"]
        close["fee"] = close["filled"] * close["fill_price"] * FUTURES_TAKER_FEE_RATE
        if trade_logger is None:
            continue
        now = datetime.now(timezone.utc)
        trade_logger.log_trade(timestamp=now, source="kill_switch", exchange="kraken_futures", pair=_RUNTIME_SYMBOLS.get(close["venue_symbol"], close["venue_symbol"]),
                               side=close["side"], price=close["fill_price"], size=close["filled"], fee=close["fee"], role_maker_taker="taker", strategy_id="kill_switch")
        for kind, amount in (("REALIZED_PNL", close["realized_pnl"]), ("TRADING_FEE", -close["fee"])):
            if amount:
                trade_logger.log_derivative_event(timestamp=now, venue_symbol=close["venue_symbol"], transaction_type=kind, amount=amount, currency="USD",
                                                  source="kill_switch", metadata={"fee_estimated": kind == "TRADING_FEE", "cli_ord_id": close["cli_ord_id"]})

    open_orders = client.call("GET", "openorders").get("openOrders", [])
    return {"cancelled": cancelled, "closes": closes, "remaining_positions": remaining, "open_orders_left": len(open_orders), "waited_seconds": waited}
