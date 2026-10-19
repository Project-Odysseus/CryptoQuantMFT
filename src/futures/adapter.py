"""The futures ledger the portfolio engine trades through: orders wait for a slot, then fill on paper or go into the order file.

The engine sends an order whenever it decides, which for daily bars is in the evening. A futures order may only go
out when its market is liquid and the desk's slot has come up, so the ledger keeps it queued and answers
`SUBMITTED`; the fill reaches the engine later through `settle_orders`, the way a live exchange's fills do.

Two modes:

- **paper**: at release the order fills at the contract's latest traded price moved by the market's cost in ticks.
- **file**: at release the orders are written to one CSV for the desk's system, and the book changes only when the
  fills file reports what was done. No order leaves this code.

The ledger also owns the contracts: the engine sees one position per market, the ledger holds it in a specific
contract month and moves it to the next one before expiry (a `roll` event the engine books).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from src.execution.adapters import ExecutionReport
from src.futures.contracts import Contract, Market, contract_to_hold, parse_code, scheduled_contract
from src.futures.liquidity import LiquidityRules, allowed_contracts, average_volume
from src.futures.order_file import FileOrder, order_file_name, read_fills, write_orders
from src.futures.quotes import DailyStore
from src.futures.sessions import Sessions, Slot, liquid_window, releasable, release_time

MAX_PRICE_AGE = timedelta(minutes=45)  # a paper fill needs a price this fresh; the free source is 10 to 15 minutes behind
FILL_GRACE = timedelta(hours=2)  # how long after its window an order sent by file may still be reported filled before it is written off
KEEP_REPORTED = 500
Prices = Callable[[str], tuple[float, datetime] | None]


class ContractBoard:
    """Which contract month each market's position is in. The price feed and the ledger share one board, so the
    price the book is marked at is always the contract it holds."""

    def __init__(self, markets: Mapping[str, Market], sessions: Sessions, path: Path | str | None = None) -> None:
        self.markets, self.sessions = dict(markets), sessions
        self.path = Path(path) if path else None
        self._codes: dict[str, str] = json.loads(self.path.read_text()) if self.path and self.path.exists() else {}

    def current(self, market_id: str, day: date) -> Contract:
        """The contract held for this market (the calendar's choice the first time it is asked)."""
        market = self.markets[market_id]
        if market_id in self._codes:
            return parse_code(market, self._codes[market_id], near=day)
        contract = scheduled_contract(market, day, self.sessions)
        self.advance(market_id, contract)
        return contract

    def due(self, market_id: str, day: date, volumes: Mapping[str, float] | None = None) -> Contract | None:
        """The contract the position should move to today, or None when it stays."""
        current = self.current(market_id, day)
        target = contract_to_hold(self.markets[market_id], day, self.sessions, current=current, volumes=volumes)
        return target if target != current else None

    def advance(self, market_id: str, contract: Contract) -> None:
        """Record that the market's position is now in `contract`."""
        self._codes[market_id] = contract.code
        if self.path:
            _write_json(self.path, self._codes)

    def codes(self) -> dict[str, str]:
        """Contract code per market, as recorded."""
        return dict(self._codes)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=1, sort_keys=True))
    temporary.replace(path)


class SessionFuturesAdapter:
    """One futures ledger (all contracts of one exchange and currency) for the portfolio engine.

    Sizes and positions at this boundary are in the engine's units (index points, euros); inside, the ledger counts
    whole contracts per contract month.
    """

    session_bound = True  # the engine calls `on_market_update` every cycle and books the events it returns
    live = False

    def __init__(self, *, venue: str, markets: Mapping[str, Market], board: ContractBoard, sessions: Sessions, slots: Sequence[Slot],
                 rules: LiquidityRules, mode: str = "paper", book_name: str = "book", prices: Prices | None = None, store: DailyStore | None = None,
                 state_path: Path | str | None = None, order_dir: Path | str | None = None, fills_dir: Path | str | None = None,
                 max_contracts: Mapping[str, int] | None = None, no_trade: Sequence[str] = ()) -> None:
        """`markets` is by symbol (the contract root). `prices` gives the latest traded price per quote symbol; without
        it (tests, mock runs) a paper order fills at the engine's own mark."""
        if mode not in ("paper", "file"):
            raise ValueError("mode must be 'paper' or 'file'")
        if mode == "file" and (order_dir is None or fills_dir is None):
            raise ValueError("file mode needs order_dir and fills_dir")
        self.exchange_name = venue
        self.markets, self.board, self.sessions, self.slots, self.rules = dict(markets), board, sessions, tuple(slots), rules
        self.mode, self.book_name, self.prices, self.store = mode, book_name, prices, store
        self.state_path = Path(state_path) if state_path else None
        self.order_dir, self.fills_dir = (Path(order_dir) if order_dir else None), (Path(fills_dir) if fills_dir else None)
        self.max_contracts, self.no_trade = dict(max_contracts or {}), set(no_trade)
        self._marks: dict[str, float] = {}
        state = json.loads(self.state_path.read_text()) if self.state_path and self.state_path.exists() else {}
        self.positions: dict[str, dict[str, int]] = state.get("positions", {})
        self.orders: dict[str, dict[str, Any]] = state.get("orders", {})
        self.outcomes: list[dict[str, Any]] = state.get("outcomes", [])
        self.reported: dict[str, list[dict[str, Any]]] = state.get("reported", {})
        self.applied_fills: list[str] = state.get("applied_fills", [])
        self.rolls: dict[str, dict[str, Any]] = state.get("rolls", {})
        self.noted: list[str] = state.get("noted", [])
        self._tracked: set[str] = set()

    # --- what the engine calls ---------------------------------------------------------------------------------------

    def submit_order(self, *, order_id: str, side: str, size: float, price: float, timestamp: datetime, symbol: str | None = None,
                     reduce_only: bool = False) -> ExecutionReport:
        """Queue an order for its market's next slot. The fill, or the reason there was none, comes from `settle_orders`."""
        market = self.markets.get(str(symbol))
        if market is None:
            return ExecutionReport(order_id=order_id, status="REJECTED", message=f"{symbol} is not a market of {self.exchange_name}")
        if order_id in self.orders or order_id in self.reported:
            return ExecutionReport(order_id=order_id, status="SUBMITTED", message="already received")
        contracts = market.contracts_for(size)
        if side not in ("buy", "sell") or contracts <= 0 or contracts != contracts.to_integral_value():
            return ExecutionReport(order_id=order_id, status="REJECTED", message=f"{size} {symbol} is not a whole number of contracts ({market.multiplier} each)")
        wanted, held = int(contracts), self.contracts_held(str(symbol))
        if reduce_only:
            if held == 0 or (held > 0) != (side == "sell"):
                return ExecutionReport(order_id=order_id, status="REJECTED", message="reduce-only order with nothing to reduce")
            wanted = min(wanted, abs(held))
        else:
            if market.id in self.no_trade:
                return ExecutionReport(order_id=order_id, status="REJECTED", message=f"{market.id} is on the no-trade list: only reductions")
            cap = self.max_contracts.get(market.id)
            after = held + (wanted if side == "buy" else -wanted)
            if cap is not None and abs(after) > cap and abs(after) > abs(held):
                return ExecutionReport(order_id=order_id, status="REJECTED", message=f"would hold {after:+d} contracts of {market.id}; the limit is {cap}")
        release = release_time(market, timestamp, self.slots, self.sessions)
        if release is None and releasable(market, timestamp, self.slots, self.sessions) is None:
            return ExecutionReport(order_id=order_id, status="REJECTED", message=f"no slot falls inside the liquid hours of {market.id}")
        self.orders[order_id] = {"order_id": order_id, "symbol": str(symbol), "side": side, "contracts": wanted, "reference_price": float(price),
                                 "reduce_only": bool(reduce_only), "submitted_at": timestamp.isoformat(), "state": "queued", "filled": 0,
                                 "release_at": release[0].isoformat() if release else timestamp.isoformat(), "slot": release[1].name if release else ""}
        self._save()
        return ExecutionReport(order_id=order_id, status="SUBMITTED", message=f"queued for the {self.orders[order_id]['slot'] or 'current'} slot")

    def track_order(self, *, order_id: str, symbol: str, side: str, size: float, price: float, timestamp: datetime) -> None:
        """Ask about an order after a restart: `settle_orders` then repeats its outcome, or says it never arrived."""
        self._tracked.add(order_id)

    def settle_orders(self) -> list[dict[str, Any]]:
        """Each order's outcome, once: fills (possibly in parts), CANCELED when it ended unfilled, and `never_received`
        for an order the engine recorded but this ledger never got. A queued or sent order has no outcome yet."""
        settled = list(self.outcomes)
        reported_now = {item["order_id"] for item in settled}
        for item in settled:
            self.reported.setdefault(item["order_id"], []).append(item)
        for order_id in sorted(self._tracked - reported_now):
            if order_id in self.reported:
                # Reported before the restart, and the engine lost it. Only the last part is repeated: earlier parts of
                # an order filled in pieces were most likely saved, and the engine's own check of book against ledger
                # catches the rare case where that guess is wrong.
                settled.append(self.reported[order_id][-1])
            elif order_id not in self.orders:
                settled.append({"order_id": order_id, "status": "CANCELED", "never_received": True})
        self._tracked.clear()
        self.outcomes = []
        for stale in list(self.reported)[:-KEEP_REPORTED]:
            del self.reported[stale]
        if settled:
            self._save()
        return settled

    def position_size(self, symbol: str) -> float:
        """The position in a market in the engine's units, over all its contract months."""
        market = self.markets[symbol]
        return float(self.contracts_held(symbol) * market.multiplier)

    def on_market_update(self, *, prices: Mapping[str, float], timestamp: datetime) -> list[dict[str, Any]]:
        """Read fills (file mode), roll contracts that are due, and release the orders whose market has reached a slot."""
        self._marks.update({symbol: float(price) for symbol, price in prices.items()})
        events: list[dict[str, Any]] = []
        if self.mode == "file":
            self._read_fills(events, timestamp)
            self._expire_sent(events, timestamp)
        for symbol in sorted(self.markets):
            self._roll_if_due(symbol, events, timestamp)
        self._release(events, timestamp)
        self._save()
        return events

    # --- positions ---------------------------------------------------------------------------------------------------

    def contracts_held(self, symbol: str) -> int:
        """Signed contracts held in a market, over all its contract months."""
        return sum(self.positions.get(symbol, {}).values())

    def _change(self, symbol: str, code: str, contracts: int) -> None:
        held = self.positions.setdefault(symbol, {})
        held[code] = held.get(code, 0) + contracts
        if held[code] == 0:
            del held[code]

    def _today(self, market: Market, now: datetime) -> date:
        return now.astimezone(ZoneInfo(market.timezone)).date()

    def _price(self, contract: Contract, symbol: str, now: datetime, fallback: float | None = None) -> float | None:
        """The price to fill at: the contract's latest trade if fresh, the engine's mark when no price source is set."""
        if self.prices is None:
            return self._marks.get(symbol, fallback)
        quote = self.prices(contract.price_symbol) if contract.price_symbol else None
        if quote is None or abs(now - quote[1]) > MAX_PRICE_AGE:
            return None
        return float(quote[0])

    def _average_volume(self, code: str) -> float | None:
        return average_volume(self.store.volumes(code), self.rules.volume_days) if self.store is not None else None

    def _note_once(self, key: str, event: dict[str, Any], events: list[dict[str, Any]]) -> None:
        if key not in self.noted:
            self.noted.append(key)
            del self.noted[:-KEEP_REPORTED]
            events.append(event)

    # --- releasing orders --------------------------------------------------------------------------------------------

    def _release(self, events: list[dict[str, Any]], now: datetime) -> None:
        rows: list[FileOrder] = []
        slots: set[str] = set()
        for order_id, order in sorted(self.orders.items(), key=lambda pair: pair[1]["submitted_at"]):
            if order["state"] != "queued" or order["symbol"] in self.rolls:
                continue  # a roll in progress goes first: the order would otherwise land in the contract being left
            market = self.markets[order["symbol"]]
            slot = releasable(market, now, self.slots, self.sessions)
            if slot is None:
                continue
            contract = self.board.current(market.id, self._today(market, now))
            allowed, why = allowed_contracts(order["contracts"], self._average_volume(contract.code), self.rules, reducing=order["reduce_only"])
            if why and why != "volume unknown":
                events.append({"type": "liquidity", "symbol": order["symbol"], "order_id": order_id, "problem": allowed == 0,
                               "message": f"{order_id}: {order['side']} {order['contracts']} {contract.code} cut to {allowed}: {why}"})
            if allowed == 0:
                self._finish(order_id, {"order_id": order_id, "status": "CANCELED"})
                continue
            if self.mode == "paper":
                price = self._price(contract, order["symbol"], now, order["reference_price"])
                if price is None:
                    self._note_once(f"no_price:{order_id}", {"type": "no_price", "symbol": order["symbol"], "order_id": order_id, "problem": True,
                                                             "message": f"{order_id}: no fresh price for {contract.code}; the order waits"}, events)
                    continue
                fill_price = float(market.slipped(price, order["side"]))
                self._change(order["symbol"], contract.code, allowed if order["side"] == "buy" else -allowed)
                self._finish(order_id, {"order_id": order_id, "status": "FILLED", "filled_size": float(allowed * market.multiplier), "fill_price": fill_price,
                                        "fee": 0.0, "contract": contract.code})
            else:
                window = liquid_window(market, self._today(market, now), self.sessions)
                order.update(state="sent", contract=contract.code, contracts=allowed, sent_at=now.isoformat(), valid_until=window[1].isoformat() if window else now.isoformat())
                rows.append(self._file_order(order_id, market, contract, order["side"], allowed, slot.name, now, order["valid_until"], "rebalance" if not order["reduce_only"] else "reduce"))
                slots.add(slot.name)
        self._write_file(rows, slots, events, now)

    def _file_order(self, order_id: str, market: Market, contract: Contract, side: str, contracts: int, slot: str, now: datetime, valid_until: str, reason: str) -> FileOrder:
        return FileOrder(order_id=order_id, book=self.book_name, created_utc=now.astimezone(timezone.utc).isoformat(timespec="seconds"), slot=slot, market=market.id,
                         contract=contract.code, exchange=market.exchange, side=side.upper(), quantity=contracts, order_type="MKT", limit_price="",
                         currency=market.currency, valid_until_utc=valid_until, reason=reason)

    def _write_file(self, rows: list[FileOrder], slots: set[str], events: list[dict[str, Any]], now: datetime) -> None:
        if not rows or self.order_dir is None:
            return
        name = order_file_name(f"{self.book_name}_{self.exchange_name}", now, "-".join(sorted(slots)) or "slot")
        path = write_orders(self.order_dir / name, rows)
        events.append({"type": "order_file", "message": f"wrote {len(rows)} order(s) to {path}", "path": str(path), "orders": [row.order_id for row in rows]})

    def _finish(self, order_id: str, outcome: dict[str, Any]) -> None:
        self.orders.pop(order_id, None)
        self.outcomes.append(outcome)

    # --- fills from the desk (file mode) -----------------------------------------------------------------------------

    def _read_fills(self, events: list[dict[str, Any]], now: datetime) -> None:
        for fill in read_fills(self.fills_dir) if self.fills_dir and self.fills_dir.exists() else []:
            key = f"{fill['file']}:{fill['line']}"
            if key in self.applied_fills:
                continue
            if "error" in fill:
                self.applied_fills.append(key)
                events.append({"type": "fill_unreadable", "problem": True, "message": fill["error"]})
                continue
            order_id = fill["order_id"]
            if order_id.startswith("roll-"):
                if not self._roll_fill(fill, events, now):
                    continue  # not one of this ledger's rolls: another ledger reads the same folder
                self.applied_fills.append(key)
                continue
            order = self.orders.get(order_id)
            if order is None or order["state"] not in ("sent", "expired"):
                continue  # another ledger's order, or one not sent yet; a fill for an order nobody knows stays unapplied
            market = self.markets[order["symbol"]]
            signed = fill["quantity"] if fill["side"] == "buy" else -fill["quantity"]
            self._change(order["symbol"], fill["contract"], signed)
            order["filled"] += fill["quantity"]
            done = order["filled"] >= order["contracts"]
            self.outcomes.append({"order_id": order_id, "status": "FILLED" if done else "PARTIAL", "filled_size": float(fill["quantity"] * market.multiplier),
                                  "fill_price": fill["price"], "fee": fill["fee"], "contract": fill["contract"]})
            if done:
                self.orders.pop(order_id, None)
            self.applied_fills.append(key)
        del self.applied_fills[:-5000]

    def _expire_sent(self, events: list[dict[str, Any]], now: datetime) -> None:
        """An order sent by file with no complete fill two hours after its window: tell the engine it ended.

        The record stays (state "expired"), so a fill reported later is still booked.
        """
        for order_id, order in list(self.orders.items()):
            if order["state"] == "sent" and now > datetime.fromisoformat(order["valid_until"]) + FILL_GRACE:
                order["state"] = "expired"
                self.outcomes.append({"order_id": order_id, "status": "CANCELED"})
                events.append({"type": "order_unfilled", "symbol": order["symbol"], "order_id": order_id, "problem": True,
                               "message": f"{order_id}: {order['filled']} of {order['contracts']} {order['contract']} reported filled by the end of its window"})

    # --- rolls -------------------------------------------------------------------------------------------------------

    def _roll_if_due(self, symbol: str, events: list[dict[str, Any]], now: datetime) -> None:
        market = self.markets[symbol]
        if symbol in self.rolls:
            started = datetime.fromisoformat(self.rolls[symbol]["started_at"])
            if now - started > timedelta(days=1):
                self._note_once(f"roll_open:{symbol}:{self.rolls[symbol]['to']}", {"type": "roll_incomplete", "symbol": symbol, "problem": True,
                                "message": f"{market.id}: the roll to {self.rolls[symbol]['to']} has not been reported filled after a day"}, events)
            return
        today = self._today(market, now)
        current = self.board.current(market.id, today)
        volumes = self.store.latest_volumes([current.code, current.following().code]) if self.store is not None else None
        target = self.board.due(market.id, today, volumes)
        if target is None:
            return
        held = self.positions.get(symbol, {}).get(current.code, 0)
        if held == 0:
            self.board.advance(market.id, target)
            events.append({"type": "contract_change", "symbol": symbol, "message": f"{market.id}: now trading {target.code} (was {current.code}); no position to move"})
            return
        slot = releasable(market, now, self.slots, self.sessions)
        if slot is None:
            return
        if self.mode == "file":
            close_side, open_side = ("sell", "buy") if held > 0 else ("buy", "sell")
            window = liquid_window(market, today, self.sessions)
            until = window[1].isoformat() if window else now.isoformat()
            prefix = f"roll-{current.code}-{target.code}"
            rows = [self._file_order(f"{prefix}-close", market, current, close_side, abs(held), slot.name, now, until, "roll"),
                    self._file_order(f"{prefix}-open", market, target, open_side, abs(held), slot.name, now, until, "roll")]
            self.rolls[symbol] = {"from": current.code, "to": target.code, "contracts": held, "started_at": now.isoformat(),
                                  "legs": {"close": {"filled": 0, "value": 0.0, "fee": 0.0}, "open": {"filled": 0, "value": 0.0, "fee": 0.0}}}
            self._write_file(rows, {slot.name}, events, now)
            return
        old_price, new_price = self._price(current, symbol, now), self._price(target, symbol, now)
        if old_price is None or new_price is None:
            self._note_once(f"roll_price:{symbol}:{target.code}:{today}", {"type": "no_price", "symbol": symbol, "problem": True,
                            "message": f"{market.id}: no fresh price to roll {current.code} to {target.code}; trying again"}, events)
            return
        close_side, open_side = ("sell", "buy") if held > 0 else ("buy", "sell")
        self._complete_roll(symbol, current.code, target, held, float(market.slipped(old_price, close_side)), float(market.slipped(new_price, open_side)), 0.0, events)

    def _complete_roll(self, symbol: str, from_code: str, target: Contract, held: int, from_price: float, to_price: float, fee: float, events: list[dict[str, Any]]) -> None:
        market = self.markets[symbol]
        self._change(symbol, from_code, -held)
        self._change(symbol, target.code, held)
        self.board.advance(market.id, target)
        events.append({"type": "roll", "symbol": symbol, "from_contract": from_code, "to_contract": target.code, "units": float(held * market.multiplier),
                       "contracts": held, "from_price": from_price, "to_price": to_price, "fee": fee})

    def _roll_fill(self, fill: Mapping[str, Any], events: list[dict[str, Any]], now: datetime) -> bool:
        """Add one reported fill to the roll it belongs to; True when it was one of this ledger's."""
        for symbol, roll in list(self.rolls.items()):
            prefix = f"roll-{roll['from']}-{roll['to']}-"
            if not fill["order_id"].startswith(prefix):
                continue
            leg = roll["legs"].get(fill["order_id"][len(prefix):])
            if leg is None:
                return False
            leg["filled"] += fill["quantity"]
            leg["value"] += fill["quantity"] * fill["price"]
            leg["fee"] += fill["fee"]
            size = abs(roll["contracts"])
            if all(part["filled"] >= size for part in roll["legs"].values()):
                market = self.markets[symbol]
                target = parse_code(market, roll["to"], near=now.date())
                close, opened = roll["legs"]["close"], roll["legs"]["open"]
                del self.rolls[symbol]
                self._complete_roll(symbol, roll["from"], target, roll["contracts"], close["value"] / close["filled"], opened["value"] / opened["filled"],
                                    close["fee"] + opened["fee"], events)
            return True
        return False

    # --- state -------------------------------------------------------------------------------------------------------

    def _save(self) -> None:
        if self.state_path is None:
            return
        _write_json(self.state_path, {"positions": self.positions, "orders": self.orders, "outcomes": self.outcomes, "reported": self.reported,
                                      "applied_fills": self.applied_fills, "rolls": self.rolls, "noted": self.noted})

    def describe(self) -> dict[str, Any]:
        """Contracts held and orders waiting, for the dashboard and the logs."""
        return {"mode": self.mode, "positions": {symbol: dict(held) for symbol, held in self.positions.items() if held},
                "waiting": [{key: order[key] for key in ("order_id", "symbol", "side", "contracts", "state", "release_at", "slot")} for order in self.orders.values()],
                "rolls": {symbol: {"from": roll["from"], "to": roll["to"]} for symbol, roll in self.rolls.items()}, "contracts": self.board.codes()}
