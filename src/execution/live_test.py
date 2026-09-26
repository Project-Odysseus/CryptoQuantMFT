"""A live plumbing test on Kraken Futures: open the smallest possible position, close it, and check every step.

Before real money runs through the portfolio, one round trip at the minimum
size proves the whole chain with real credentials: signing, order
placement, fills found by client id, positions and margin read back from
Kraken, the tax ledger, the trade log and Telegram. It costs two taker fees
on the minimum size (about 0.01 USD for 0.0001 BTC), plus whatever the price
does in the few seconds the position is open.

It refuses to run unless the account is flat in the tested contract, so it can
never add to, or close, a position that something else holds.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from src.execution.kraken_futures_cross import KrakenFuturesCrossMarginAdapter

SETTLE_TIMEOUT_SECONDS = 30.0


class LiveTestError(RuntimeError):
    """The live test stopped before finishing; the message says where and what state the account is in."""


def _settle(adapter: KrakenFuturesCrossMarginAdapter, order_id: str, sleep: Callable[[float], None], timeout: float) -> dict[str, Any]:
    waited = 0.0
    filled: dict[str, Any] = {"filled_size": 0.0, "fill_price": 0.0, "fee": 0.0}
    while waited <= timeout:
        for item in adapter.settle_orders():
            if item["order_id"] != order_id:
                continue
            if item.get("filled_size"):
                total = filled["filled_size"] + item["filled_size"]
                filled["fill_price"] = (filled["fill_price"] * filled["filled_size"] + item["fill_price"] * item["filled_size"]) / total
                filled["filled_size"], filled["fee"] = total, filled["fee"] + item["fee"]
            if item["status"] in {"FILLED", "CANCELED"}:
                return {**filled, "status": item["status"]}
        sleep(1.0)
        waited += 1.0
    return {**filled, "status": "UNSETTLED"}


def run_futures_live_test(
    adapter: KrakenFuturesCrossMarginAdapter,
    *,
    symbol: str,
    mark_price: float,
    trade_logger: Any | None = None,
    notifier: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = SETTLE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Buy the contract's minimum size, confirm it on Kraken, sell it reduce-only, confirm flat, and record both fills.

    Returns a summary (fills, fees, realized P&L, account equity before and
    after). Raises `LiveTestError` with the account's state if any step fails.
    The exchange's positions are always re-read, so a failure message says
    whether a position is still open.
    """
    contract = adapter.contracts[symbol]
    size = contract.min_size
    started = datetime.now(timezone.utc)
    before = adapter.sync_account()
    if before["positions"].get(symbol):
        raise LiveTestError(f"the account already holds {before['positions'][symbol]} {symbol}; the test only runs from flat")
    needed = size * mark_price / adapter.max_leverage
    if before["available_margin"] is not None and before["available_margin"] < needed * 1.5:
        raise LiveTestError(f"available margin {before['available_margin']:.2f} is below what {size} {symbol} needs ({needed:.2f} at {adapter.max_leverage:g}x, plus a buffer)")

    steps: list[dict[str, Any]] = []
    for step, side, reduce_only in (("open", "buy", False), ("close", "sell", True)):
        order_id = f"livetest-{started:%Y%m%dT%H%M%S}-{step}"
        report = adapter.submit_order(order_id=order_id, side=side, size=size, price=mark_price, timestamp=datetime.now(timezone.utc), symbol=symbol, reduce_only=reduce_only)
        if report.status == "REJECTED":
            state = adapter.sync_account()["positions"].get(symbol, 0.0)
            raise LiveTestError(f"the {step} order was rejected ({report.message}); {symbol} position on Kraken is now {state}")
        settled = _settle(adapter, order_id, sleep, timeout)
        state = adapter.sync_account()
        position = state["positions"].get(symbol, 0.0)
        expected = size if step == "open" else 0.0
        if settled["status"] != "FILLED" or abs(position - expected) > contract.size_step / 2:
            raise LiveTestError(f"the {step} order ended {settled['status']} with {settled['filled_size']} filled; {symbol} position on Kraken is {position} "
                                f"(expected {expected}). Check the account before doing anything else.")
        steps.append({"step": step, "side": side, "order_id": order_id, "client_id": adapter.client_order_id(order_id), **settled, "position_after": position})
        if trade_logger is not None:
            trade_logger.log_trade(timestamp=datetime.now(timezone.utc), source="live_test", exchange="kraken_futures", pair=symbol, side=side,
                                   price=settled["fill_price"], size=settled["filled_size"], fee=settled["fee"], strategy_id="live_test")

    opened, closed = steps
    realized = (closed["fill_price"] - opened["fill_price"]) * size
    fees = opened["fee"] + closed["fee"]
    after = adapter.sync_account()
    summary = {"symbol": symbol, "size": size, "open_price": opened["fill_price"], "close_price": closed["fill_price"], "realized_pnl": realized,
               "fees_estimated": fees, "equity_before": before["equity"], "equity_after": after["equity"], "steps": steps,
               "venue_symbol": contract.venue_symbol, "currency": contract.collateral_currency}
    if trade_logger is not None:
        now = datetime.now(timezone.utc)
        for kind, amount in (("REALIZED_PNL", realized), ("TRADING_FEE", -fees)):
            if amount:
                trade_logger.log_derivative_event(timestamp=now, venue_symbol=contract.venue_symbol, transaction_type=kind, amount=amount,
                                                  currency=contract.collateral_currency, source="live_test", metadata={"fee_estimated": kind == "TRADING_FEE"})
        trade_logger.log_event(timestamp=now, level="WARNING", event_type="futures_live_test", message=f"live round trip of {size} {symbol} completed", source="live_test", metadata=summary)
    if notifier is not None:
        notifier.send_alert(event_type="futures_live_test", message=f"[LIVE] Round trip of {size} {symbol} on Kraken Futures completed: bought at {opened['fill_price']:,.1f}, "
                            f"sold at {closed['fill_price']:,.1f}, P&L {realized:+.4f}, fees ~{fees:.4f} {contract.collateral_currency}.", metadata={})
    return summary
