"""The portfolio engine: one cycle from new bars to fills, the only portfolio module that talks to adapters and the logger.

Each `run_cycle` call gets every completed bar so far per (instrument,
interval). It then:

1. marks the book and the adapters to the latest grid-bar closes (the adapters
   accrue funding, and the book books the same payments);
2. steps each sleeve through its bars that completed since the last cycle
   (`SleeveRunner.step`, the same code research replays);
3. on each new grid bar (the shortest sleeve interval), steps the allocator,
   and on the latest one decides: allocate, net, apply the portfolio risk
   overlay from the book's equity, peak, day start and current weights, and
   plan orders;
4. sends the orders to each venue's adapter (reduce-only where planned), books
   the fills, and reconciles the book with every adapter per instrument;
5. logs fills with the sleeve that drove them as `strategy_id`, logs the
   decision and any mismatch as operational events, sends one Telegram message
   per fill, and saves an atomic JSON checkpoint.

A cycle without a new grid bar only marks the book: nothing is decided twice,
so repeating a cycle can't double an order. On the first cycle, every sleeve
and the allocator replay the history they are given (warmup) and only the
latest bar is traded.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from src.execution.adapters import ExecutionAdapter, SandboxExecutionAdapter
from src.execution.cross_margin import SandboxCrossMarginPerpAdapter
from src.execution.perps import assumed_perp_contract
from src.portfolio.allocation import Allocator
from src.portfolio.book import PortfolioBook
from src.portfolio.config import PortfolioConfig
from src.portfolio.netting import net_targets
from src.portfolio.orders import PlannedOrder, SkippedChange, plan_orders
from src.portfolio.risk import RiskAction, apply_portfolio_risk
from src.portfolio.sleeves import SleeveDecision, SleeveRunner, SleeveState, _Prefix
from src.runtime.config import BAR_INTERVALS
from src.utils.telegram import TradeAlert

BarsByKey = Mapping[tuple[str, str], Sequence[Any]]  # (instrument id, interval) -> completed bars, oldest first


@dataclass(slots=True)
class CycleReport:
    """What one cycle saw, decided and did."""

    timestamp: datetime
    decided: bool
    equity: float
    sleeve_decisions: dict[str, SleeveDecision] = field(default_factory=dict)
    targets: dict[str, float] = field(default_factory=dict)  # net per instrument, before the risk overlay
    adjusted: dict[str, float] = field(default_factory=dict)  # after the risk overlay
    risk_actions: list[RiskAction] = field(default_factory=list)
    orders: list[PlannedOrder] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[SkippedChange] = field(default_factory=list)
    mismatches: dict[str, dict[str, float]] = field(default_factory=dict)
    adapter_events: list[dict[str, Any]] = field(default_factory=list)


def build_paper_adapters(
    config: PortfolioConfig,
    book: PortfolioBook,
    *,
    state_dir: str | Path | None = None,
    funding_pct_per_day: float = 0.01,
) -> dict[str, ExecutionAdapter]:
    """Sandbox adapters for paper trading a config, funded with the book's cash per venue.

    Perp venues get one `SandboxCrossMarginPerpAdapter` holding every perp
    on that venue (the leverage cap is the highest `max_leverage` among
    them; fees, lot steps and slippage come from each instrument). Spot
    venues get a `SandboxExecutionAdapter` at the instrument's taker fee.
    """
    adapters: dict[str, ExecutionAdapter] = {}
    by_venue: dict[str, list[Any]] = {}
    for spec in config.instruments.values():
        by_venue.setdefault(spec.venue, []).append(spec)
    for venue, specs in by_venue.items():
        cash = float(book.cash.get(venue, Decimal(0)))
        if all(spec.kind == "perp" for spec in specs):
            contracts = []
            for spec in specs:
                contract = assumed_perp_contract(spec.symbol)
                step = spec.lot_step or contract.size_step
                contracts.append(replace(contract, taker_fee_rate=spec.taker_fee_pct / 100.0, size_step=step, min_size=max(spec.min_order_size, step),
                                         max_leverage=max(contract.max_leverage, spec.max_leverage)))
            adapters[venue] = SandboxCrossMarginPerpAdapter(
                contracts=contracts, starting_collateral=cash, max_leverage=max(spec.max_leverage for spec in specs),
                funding_pct_per_day=funding_pct_per_day, slippage_bps={spec.symbol: spec.slippage_bps for spec in specs}, exchange_name=venue,
                state_path=Path(state_dir) / f"paper_{venue}.json" if state_dir else None,
            )
        elif all(spec.kind == "spot" for spec in specs):
            if len({spec.taker_fee_pct for spec in specs}) > 1:
                raise ValueError(f"spot instruments on {venue} have different fees; the spot sandbox takes one fee rate")
            adapter = SandboxExecutionAdapter(exchange_name=venue, fee_rate=specs[0].taker_fee_pct / 100.0)
            adapter._balances = {adapter._base_currency: cash}
            adapters[venue] = adapter
        else:
            raise ValueError(f"venue {venue} mixes spot and perp instruments; give them separate venue names")
    return adapters


def _accepts(function: Any, name: str) -> bool:
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


class PortfolioEngine:
    """Runs a portfolio config cycle by cycle against one execution adapter per venue."""

    def __init__(
        self,
        config: PortfolioConfig,
        *,
        adapters: Mapping[str, ExecutionAdapter],
        book: PortfolioBook,
        trade_logger: Any | None = None,
        notifier: Any | None = None,
        strategies: Mapping[str, Any] | None = None,
        mode: str = "paper",
        state_path: str | Path | None = None,
    ) -> None:
        """Wire the sleeves, allocator and book; restore the checkpoint at `state_path` if there is one.

        Raises when a venue in the config has no adapter, so a wiring mistake
        fails at startup rather than at the first order.
        """
        missing = sorted({spec.venue for spec in config.instruments.values()} - set(adapters))
        if missing:
            raise ValueError(f"no execution adapter for venue(s) {missing}")
        self.config = config
        self.adapters = dict(adapters)
        self.book = book
        self.trade_logger = trade_logger
        self.notifier = notifier
        self.mode = mode
        self.state_path = Path(state_path) if state_path else None
        strategies = dict(strategies or {})
        self.sleeves = {spec.id: spec for spec in config.enabled_sleeves}
        self.runners = {sleeve_id: SleeveRunner(spec, strategy=strategies.get(sleeve_id)) for sleeve_id, spec in self.sleeves.items()}
        self.grid_interval = min((spec.interval for spec in self.sleeves.values()), key=lambda interval: BAR_INTERVALS[interval])
        self.grid_step = timedelta(seconds=BAR_INTERVALS[self.grid_interval])
        per_day = 86400 / BAR_INTERVALS[self.grid_interval]
        self.allocator = Allocator(config.budgets(), config.allocation, lookback=max(2, round(config.allocation_lookback_days * per_day)),
                                   refit_every=max(1, round(config.allocation_refit_days * per_day)))
        self.states = {sleeve_id: SleeveState() for sleeve_id in self.sleeves}
        self.last_sleeve_bar: dict[str, datetime] = {}
        self.last_grid_bar: datetime | None = None
        self.last_grid_close: dict[str, float] = {}
        self.cycle = 0
        self.restored = self._load()

    # --- the cycle -----------------------------------------------------------------------------------------------

    def run_cycle(self, bars: BarsByKey, *, now: datetime, fx: Mapping[str, float] | None = None, stale: Sequence[str] = ()) -> CycleReport:
        """Process newly completed bars and, on a new grid bar, decide and trade. See the module docstring."""
        self.cycle += 1
        instruments = sorted({spec.instrument for spec in self.sleeves.values()})
        grid = {instrument: list(bars.get((instrument, self.grid_interval), [])) for instrument in instruments}
        empty = [instrument for instrument, series in grid.items() if not series]
        if empty:
            raise ValueError(f"no {self.grid_interval} bars for {empty}")
        prices = {instrument: float(series[-1].close) for instrument, series in grid.items()}
        report = CycleReport(timestamp=now, decided=False, equity=0.0)

        report.adapter_events = self._mark_adapters(prices, now)
        self.book.mark(prices, fx=fx, now=now)

        for sleeve_id, spec in self.sleeves.items():
            series = list(bars.get((spec.instrument, spec.interval), []))
            last = self.last_sleeve_bar.get(sleeve_id)
            fresh = [index for index, bar in enumerate(series) if last is None or bar.timestamp > last]
            if not fresh:
                continue
            runner = self.runners[sleeve_id]
            signals = runner.signals(series)  # causal: element i only uses bars up to i, so one pass serves every new bar
            for index in fresh:
                self.states[sleeve_id], decision = runner.step(self.states[sleeve_id], _Prefix(series, index + 1), signals[index])
                report.sleeve_decisions[sleeve_id] = decision
            self.last_sleeve_bar[sleeve_id] = series[fresh[-1]].timestamp

        latest = max(series[-1].timestamp for series in grid.values())
        stale = sorted(set(stale) | {instrument for instrument, series in grid.items() if series[-1].timestamp < latest})
        new_grid_bars = sorted({bar.timestamp for series in grid.values() for bar in series if self.last_grid_bar is None or bar.timestamp > self.last_grid_bar})
        if not new_grid_bars:
            report.equity = float(self.book.equity())
            self._save()
            return report
        closes = {instrument: {bar.timestamp: float(bar.close) for bar in series} for instrument, series in grid.items()}
        scales: dict[str, float] = {}
        for stamp in new_grid_bars:
            returns = {}
            for sleeve_id, spec in self.sleeves.items():
                close, previous = closes[spec.instrument].get(stamp), self.last_grid_close.get(spec.instrument)
                returns[sleeve_id] = close / previous - 1.0 if close is not None and previous else float("nan")
            scales = self.allocator.step(returns)
            for instrument in instruments:
                if stamp in closes[instrument]:
                    self.last_grid_close[instrument] = closes[instrument][stamp]
        self.last_grid_bar = new_grid_bars[-1]
        report.decided = True
        self._decide_and_trade(report, scales=scales, prices=prices, stale=stale, now=now)
        report.mismatches = self.reconcile()
        report.equity = float(self.book.equity())
        self._log_cycle(report)
        self._save()
        return report

    def _mark_adapters(self, prices: Mapping[str, float], now: datetime) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for venue, adapter in self.adapters.items():
            symbols = {spec.symbol: instrument for instrument, spec in self.config.instruments.items() if spec.venue == venue}
            if isinstance(adapter, SandboxCrossMarginPerpAdapter):
                for event in adapter.on_market_update(prices={symbol: prices[instrument] for symbol, instrument in symbols.items() if instrument in prices}, timestamp=now):
                    event = {**event, "venue": venue}
                    if event["type"] == "funding":
                        event["instrument"] = symbols[event["symbol"]]
                        self.book.book_funding(event["instrument"], event["payment"])
                    elif event["type"] == "liquidation":
                        self._book_liquidation(event, symbols)
                    events.append(event)
        return events

    def _book_liquidation(self, event: dict[str, Any], symbols: Mapping[str, str]) -> None:
        """A sandbox liquidation closed positions on the exchange; close them in the book at the same fills."""
        adapter = self.adapters[event["venue"]]
        for order in adapter.list_orders():
            if order.order_id.startswith("liquidation-") and order.symbol in event["closed"] and order.order_id not in self.book.liquidations_booked:
                self.book.apply_fill(symbols[order.symbol], order.side, order.filled_size, order.fill_price, order.fee)
                self.book.liquidations_booked.append(order.order_id)

    def _decide_and_trade(self, report: CycleReport, *, scales: Mapping[str, float], prices: Mapping[str, float], stale: Sequence[str], now: datetime) -> None:
        allocated = {sleeve_id: (spec.instrument, self.states[sleeve_id].weight * scales.get(sleeve_id, 0.0)) for sleeve_id, spec in self.sleeves.items()}
        self.book.set_sleeve_targets(allocated)
        report.targets, attribution = net_targets(allocated)
        equity = float(self.book.equity())
        report.adjusted, report.risk_actions = apply_portfolio_risk(
            report.targets, config=self.config.risk, venues=self.config.venues(), can_short=self.config.can_short(),
            current=self.book.weights(), equity=equity, peak_equity=float(self.book.peak_equity),
            day_start_equity=float(self.book.day_start_equity), stale=stale,
        )
        plan = plan_orders(self.book.units(), report.adjusted, prices=prices, equity=equity, instruments=self.config.instruments, band=self.config.rebalance_band)
        report.orders, report.skipped = plan.orders, plan.skipped
        for number, order in enumerate(plan.orders):
            self._execute(order, number=number, attribution=attribution.get(order.instrument, {}), report=report, now=now)

    def _execute(self, order: PlannedOrder, *, number: int, attribution: Mapping[str, float], report: CycleReport, now: datetime) -> None:
        spec = self.config.instruments[order.instrument]
        adapter = self.adapters[spec.venue]
        order_id = f"pf-{self.cycle}-{number}-{spec.venue}-{spec.symbol}"
        extra = {"reduce_only": order.reduce_only} if _accepts(adapter.submit_order, "reduce_only") else {}
        result = adapter.submit_order(order_id=order_id, side=order.side, size=float(order.units), price=order.price, timestamp=now, symbol=spec.symbol, **extra)
        drivers = {sleeve_id: weight for sleeve_id, weight in attribution.items()}
        strategy_id = next(iter(drivers)) if len(drivers) == 1 else "portfolio"
        if result.status != "FILLED" or not result.filled_size:
            rejection = {"order_id": order_id, "instrument": order.instrument, "side": order.side, "units": str(order.units), "reason": order.reason, "message": result.message}
            report.rejected.append(rejection)
            self._event("WARNING", "portfolio_order_rejected", f"{order.side} {order.units} {order.instrument} rejected: {result.message}", rejection, now)
            return
        self.book.apply_fill(order.instrument, order.side, result.filled_size, result.fill_price, result.fee)
        fill = {"order_id": order_id, "instrument": order.instrument, "side": order.side, "units": result.filled_size, "price": result.fill_price,
                "fee": result.fee, "reason": order.reason, "reduce_only": order.reduce_only, "strategy_id": strategy_id, "sleeves": drivers}
        report.fills.append(fill)
        if self.trade_logger is not None:
            self.trade_logger.log_trade(timestamp=now, source="portfolio", exchange=spec.venue, pair=spec.symbol, side=order.side, price=float(result.fill_price),
                                        size=float(result.filled_size), fee=float(result.fee), strategy_id=strategy_id)
            self._event("INFO", "portfolio_fill", f"{order.side} {result.filled_size} {order.instrument} @ {result.fill_price} ({order.reason})", fill, now)
        if self.notifier is not None:
            self.notifier.send_trade_alert(self._trade_alert(order, fill, report, now))

    def _trade_alert(self, order: PlannedOrder, fill: Mapping[str, Any], report: CycleReport, now: datetime) -> TradeAlert:
        notes = []
        for sleeve_id, weight in fill["sleeves"].items():
            decision = report.sleeve_decisions.get(sleeve_id)
            action = f", {decision.action}" + (f" ({decision.reason})" if decision.reason else "") if decision else ""
            notes.append(f"Sleeve {sleeve_id} ({self.sleeves[sleeve_id].strategy}): target {weight:+.1%} of equity{action}")
        for action in report.risk_actions:
            if action.instrument == order.instrument:
                notes.append(f"Risk {action.rule}: {action.before:+.1%} -> {action.after:+.1%}")
        position = self.book.positions.get(order.instrument)
        equity = float(self.book.equity())
        peak = float(self.book.peak_equity)
        return TradeAlert(
            mode=self.mode, strategy_name=f"portfolio {self.config.name}", side=order.side, size=float(fill["units"]), price=float(fill["price"]),
            symbol=order.instrument, intent=order.reason, fee=float(fill["fee"]), position_size=float(position.units) if position else 0.0,
            avg_entry_price=float(position.avg_entry) if position and position.units else None, equity=equity,
            pnl=equity - float(self.book.initial_equity), drawdown_pct=max(0.0, 1.0 - equity / peak) if peak > 0 else 0.0, timestamp=now, notes=tuple(notes),
        )

    # --- reconciliation, logging, checkpoints ---------------------------------------------------------------------

    def reconcile(self) -> dict[str, dict[str, float]]:
        """Book units vs each adapter's position per instrument; returns the mismatches (empty when they agree)."""
        mismatches = {}
        book_units = self.book.units()
        for instrument, spec in self.config.instruments.items():
            adapter = self.adapters[spec.venue]
            if isinstance(adapter, SandboxCrossMarginPerpAdapter):
                exchange = adapter.position_size(spec.symbol)
            else:
                exchange = float(adapter.get_account_snapshot().get("positions", {}).get(adapter._position_symbol(spec.symbol), 0.0))
            book = float(book_units.get(instrument, 0))
            if not np.isclose(book, exchange, rtol=1e-9, atol=1e-10):
                mismatches[instrument] = {"book": book, "exchange": exchange}
        return mismatches

    def _event(self, level: str, event_type: str, message: str, metadata: Mapping[str, Any], now: datetime) -> None:
        if self.trade_logger is not None:
            self.trade_logger.log_event(timestamp=now, level=level, event_type=event_type, message=message, source="portfolio", metadata=dict(metadata))

    def _log_cycle(self, report: CycleReport) -> None:
        decision = {
            "cycle": self.cycle, "equity": report.equity, "targets": report.targets, "adjusted": report.adjusted,
            "risk_actions": [vars_of(action) for action in report.risk_actions], "skipped": [vars_of(skip) for skip in report.skipped],
            "orders": len(report.orders), "fills": len(report.fills), "rejected": len(report.rejected),
            "sleeves": {sleeve_id: {"action": decision.action, "weight": decision.weight, "reason": decision.reason} for sleeve_id, decision in report.sleeve_decisions.items()},
        }
        self._event("INFO", "portfolio_decision", f"cycle {self.cycle}: {len(report.fills)} fills, equity {report.equity:,.2f}", decision, report.timestamp)
        if report.mismatches:
            self._event("ERROR", "portfolio_reconciliation_mismatch", f"book and exchange disagree on {sorted(report.mismatches)}", report.mismatches, report.timestamp)
            if self.notifier is not None:
                self.notifier.send_alert(event_type="portfolio_reconciliation_mismatch", message="Book and exchange positions disagree; no new risk until resolved.", metadata=report.mismatches)

    def to_dict(self) -> dict[str, Any]:
        """Everything needed to resume: the book, sleeve states, allocator, and the last processed bars."""
        return {
            "config_name": self.config.name,
            "cycle": self.cycle,
            "book": self.book.to_dict(),
            "states": {sleeve_id: state.to_dict() for sleeve_id, state in self.states.items()},
            "allocator": self.allocator.to_dict(),
            "last_sleeve_bar": {sleeve_id: stamp.isoformat() for sleeve_id, stamp in self.last_sleeve_bar.items()},
            "last_grid_bar": self.last_grid_bar.isoformat() if self.last_grid_bar else None,
            "last_grid_close": self.last_grid_close,
        }

    def _save(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.to_dict(), indent=1, sort_keys=True))
        temporary.replace(self.state_path)

    def _load(self) -> bool:
        if self.state_path is None or not self.state_path.exists():
            return False
        payload = json.loads(self.state_path.read_text())
        if payload.get("config_name") != self.config.name or set(payload.get("states", {})) != set(self.sleeves):
            raise ValueError(f"{self.state_path} was written for another portfolio or sleeve set; move it aside to start fresh")
        self.cycle = int(payload["cycle"])
        self.book = PortfolioBook.from_dict(payload["book"], instruments=self.config.instruments)
        self.states = {sleeve_id: SleeveState.from_dict(state) for sleeve_id, state in payload["states"].items()}
        self.allocator = Allocator.from_dict(payload["allocator"])
        self.last_sleeve_bar = {sleeve_id: datetime.fromisoformat(stamp) for sleeve_id, stamp in payload["last_sleeve_bar"].items()}
        self.last_grid_bar = datetime.fromisoformat(payload["last_grid_bar"]) if payload.get("last_grid_bar") else None
        self.last_grid_close = {instrument: float(close) for instrument, close in payload["last_grid_close"].items()}
        return True


def vars_of(item: Any) -> dict[str, Any]:
    """A slotted dataclass as a plain dict (for event metadata)."""
    return {name: getattr(item, name) for name in item.__slots__}
