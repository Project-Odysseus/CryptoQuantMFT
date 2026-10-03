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
import time
import uuid
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
from src.portfolio.book import BookPosition, PortfolioBook
from src.portfolio.book_risk import build_risk_model, exposure_summary
from src.portfolio.config import PortfolioConfig
from src.portfolio.netting import net_targets
from src.portfolio.orders import FALLBACK_STEP, PlannedOrder, SkippedChange, plan_orders
from src.portfolio.risk import RiskAction, apply_portfolio_risk
from src.portfolio.risk_model import ReturnWindow, RiskModel
from src.portfolio.sleeves import SleeveDecision, SleeveRunner, SleeveState, _Prefix
from src.runtime.config import BAR_INTERVALS
from src.utils.telegram import TradeAlert

BarsByKey = Mapping[tuple[str, str], Sequence[Any]]  # (instrument id, interval) -> completed bars, oldest first
SLEEVE_ERROR_LIMIT = 3  # consecutive failing cycles before a sleeve is disabled until restart
LATE_FILL_WATCH = timedelta(hours=1)  # matches the live adapter's watch on written-off orders
LATE_FILL_MIN_CHECKS = 10  # ...and at least this many settle passes


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
    sleeve_errors: dict[str, str] = field(default_factory=dict)
    cooldowns: dict[str, str] = field(default_factory=dict)  # instrument -> ISO time its rejection cooldown ends
    stop_actions: list[dict[str, Any]] = field(default_factory=list)  # exchange stops placed, kept, cancelled or failed
    account_log: dict[str, Any] = field(default_factory=dict)  # what the account-log reconciliation corrected this cycle


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
        record_tax: bool = False,
        funding_source: Any | None = None,
        basket_source: Any | None = None,
    ) -> None:
        """Wire the sleeves, allocator and book; restore the checkpoint at `state_path` if there is one.

        Raises when a venue in the config has no adapter, so a wiring mistake
        fails at startup rather than at the first order.

        `record_tax` writes every realized P&L, fee and funding flow (perps)
        and every spot fill (FIFO lots) to the Norwegian tax ledger at the
        moment it happens. It is for live trading only, so paper runs never
        mix simulated flows into the real ledger.

        `funding_source(venue_symbol)` returns Kraken's hourly funding rates
        (default: the public history); a live account books funding from them.

        `basket_source(basket, now)` returns a basket's daily signal data
        (default: Binance's public API, one fetch per basket and day).
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
        self.runners = {sleeve_id: SleeveRunner(spec, strategy=strategies.get(sleeve_id)) for sleeve_id, spec in self.sleeves.items() if spec.basket is None}
        self.groups = config.allocation_groups()  # sleeve -> allocation unit (a basket's members share one)
        if basket_source is None and config.baskets:
            from src.portfolio.basket import CachedPanelSource

            basket_source = CachedPanelSource()
        self.basket_source = basket_source
        self.basket_last_decision: dict[str, str] = {}  # basket id -> the last daily close it processed (survives restarts)
        self.grid_interval = min((spec.interval for spec in self.sleeves.values()), key=lambda interval: BAR_INTERVALS[interval])
        self.grid_step = timedelta(seconds=BAR_INTERVALS[self.grid_interval])
        per_day = 86400 / BAR_INTERVALS[self.grid_interval]
        self.allocator = Allocator(config.group_budgets(), config.allocation, lookback=max(2, round(config.allocation_lookback_days * per_day)),
                                   refit_every=max(1, round(config.allocation_refit_days * per_day)), groups=self.groups)
        # The risk model: stepped with every grid bar's instrument returns, read by the overlay and the snapshot
        self.risk_model = build_risk_model(config, self.grid_interval)
        window = max(2, round(config.risk.correlation_lookback_days * per_day))
        self.unit_returns = ReturnWindow(sorted(set(self.groups.values())), length=window, min_bars=min(window, max(30, round(10 * per_day))))
        self.held_weights: dict[str, float] = {}  # each sleeve's own weight held into the current grid bar (survives restarts)
        self.states = {sleeve_id: SleeveState() for sleeve_id in self.sleeves}
        self.last_sleeve_bar: dict[str, datetime] = {}
        self.last_grid_bar: datetime | None = None
        self.last_grid_close: dict[str, float] = {}
        self.last_decisions: dict[str, dict[str, Any]] = {}  # sleeve id -> its latest decision, for the dashboard
        self.record_tax = record_tax
        self.pending_tax: list[dict[str, Any]] = []  # flows the ledger couldn't take yet (e.g. no FX rate); retried each cycle
        self.pending_orders: dict[str, dict[str, Any]] = {}  # sent to a live exchange, fill not settled yet (survives restarts)
        # Written off as unfilled, but watched for an hour in case the exchange's fill list was only late (survives restarts)
        self.written_off_orders: dict[str, dict[str, Any]] = {}
        self.unreconciled: dict[str, dict[str, float]] = {}  # book vs exchange disagreements: only reductions until resolved
        self.funding_booked_until: dict[str, str] = {}  # live: the last funding hour booked per instrument
        self.funding_source = funding_source
        self.equity_drift_alerted = False
        self.book_id = uuid.uuid4().hex[:10]  # part of every client order id; kept in the checkpoint
        self.sleeve_error_counts: dict[str, int] = {}
        self.rejection_streak: dict[str, int] = {}  # consecutive rejected orders per instrument (survives restarts)
        self.cooldown_until: dict[str, str] = {}  # instrument -> ISO time until which only reductions are sent
        # Exchange stops: per instrument the position size and stop price they were set for, and stop fills booked
        self.stop_anchors: dict[str, dict[str, Any]] = {}
        self.stop_fills_booked: list[str] = []
        self.stops_verified = False  # the first sync after a start also cancels stops left for positions now closed
        # Account-log reconciliation (live): the last log entry processed per venue, and funding per instrument in
        # "received" terms (negative = paid): realized per Kraken, estimated by the book, unrealized at the start, trued up
        self.account_log_last_id: dict[str, int] = {}
        self.funding_actual: dict[str, float] = {}
        self.funding_estimated: dict[str, float] = {}
        self.funding_baseline: dict[str, float] = {}
        self.funding_trued_up: dict[str, float] = {}
        self.unknown_fee_fills: list[str] = []
        self.stop_failure_alerted = False
        self.disabled_sleeves: set[str] = set()
        self.cycle = 0
        self.restored = self._load()
        for adapter in self.adapters.values():
            if hasattr(adapter, "client_id_prefix"):
                adapter.client_id_prefix = f"cqm-{self.book_id}"
        for order_id, meta in {**self.written_off_orders, **self.pending_orders}.items():  # re-register orders sent before a restart
            spec = self.config.instruments[meta["instrument"]]
            track = getattr(self.adapters[spec.venue], "track_order", None)
            if callable(track):
                track(order_id=order_id, symbol=spec.symbol, side=meta["side"], size=float(meta["units"]), price=float(meta["price"]),
                      timestamp=datetime.fromisoformat(meta["submitted_at"]))

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
        self._flush_tax(now)
        self._settle_pending(report, now)
        self._book_stop_fills(report, now)  # before the account sync, so a stop fill isn't taken for someone else's trade

        report.adapter_events = self._mark_adapters(prices, now)
        self._start_account_log(now)  # before any trade, so the book's own first fills are reconciled
        self.book.mark(prices, fx=fx, now=now)
        self._settle_option_expiries(prices, now)
        self._book_live_funding(now)
        self._flush_tax(now)  # funding booked while marking
        self._check_equity_drift(now)

        for sleeve_id, spec in self.sleeves.items():
            if spec.basket is not None:
                continue  # set by its basket (`_step_baskets`)
            series = list(bars.get((spec.instrument, spec.interval), []))
            last = self.last_sleeve_bar.get(sleeve_id)
            fresh = [index for index, bar in enumerate(series) if last is None or bar.timestamp > last]
            if not fresh:
                continue
            runner = self.runners[sleeve_id]
            try:
                signals = runner.signals(series)  # causal: element i only uses bars up to i, so one pass serves every new bar
                for index in fresh:
                    self.states[sleeve_id], decision = runner.step(self.states[sleeve_id], _Prefix(series, index + 1), signals[index])
                    report.sleeve_decisions[sleeve_id] = decision
                    if decision.action not in ("hold", "flat") or sleeve_id not in self.last_decisions:
                        self.last_decisions[sleeve_id] = {"action": decision.action, "reason": decision.reason, "bar": series[index].timestamp.isoformat()}
                self.sleeve_error_counts.pop(sleeve_id, None)
            except Exception as exc:  # noqa: BLE001 - one broken sleeve must not stop the book
                self._sleeve_failed(sleeve_id, exc, report, now)
            self.last_sleeve_bar[sleeve_id] = series[fresh[-1]].timestamp

        self._step_baskets(bars, report, now)

        latest = max(series[-1].timestamp for series in grid.values())
        stale = sorted(set(stale) | {instrument for instrument, series in grid.items() if series[-1].timestamp < latest})
        new_grid_bars = sorted({bar.timestamp for series in grid.values() for bar in series if self.last_grid_bar is None or bar.timestamp > self.last_grid_bar})
        if not new_grid_bars:
            self._sync_stops(prices, report, now)
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
            # the sleeves' own weights for the covariance methods; after a multi-bar catch-up they are the latest ones
            unit_scales = self.allocator.step(returns, weights={sleeve_id: float(self.states[sleeve_id].weight) for sleeve_id in self.sleeves})
            scales = {sleeve_id: unit_scales.get(self.groups.get(sleeve_id, sleeve_id), 0.0) for sleeve_id in self.sleeves}
            self.risk_model.step({spec.instrument: returns[sleeve_id] for sleeve_id, spec in self.sleeves.items()})
            if self.held_weights:  # what each strategy earned over the bar, from the weights it held into it
                earned: dict[str, float] = {}
                for sleeve_id, value in returns.items():
                    unit = self.groups.get(sleeve_id, sleeve_id)
                    held = self.held_weights.get(sleeve_id, 0.0)
                    earned[unit] = earned.get(unit, 0.0) + (held * value if held and np.isfinite(value) else 0.0)
                self.unit_returns.add(earned)
            for instrument in instruments:
                if stamp in closes[instrument]:
                    self.last_grid_close[instrument] = closes[instrument][stamp]
        self.last_grid_bar = new_grid_bars[-1]
        self.held_weights = {sleeve_id: float(self.states[sleeve_id].weight) for sleeve_id in self.sleeves}
        report.decided = True
        self._decide_and_trade(report, scales=scales, prices=prices, stale=stale, now=now)
        report.mismatches = self.reconcile()
        self.unreconciled = dict(report.mismatches)
        self._sync_stops(prices, report, now)
        self._reconcile_account_log(report, now)
        report.equity = float(self.book.equity())
        self._log_cycle(report)
        self._save()
        return report

    def _settle_option_expiries(self, prices: Mapping[str, float], now: datetime) -> None:
        """Settle options past their expiry at intrinsic value, at each coin's price from the book's perp or spot marks.

        Paper books settle here; a live Deribit account settles on the exchange, and its adapter's sync is the
        record there. The realized P&L goes to the tax ledger like any option close.
        """
        if not any(spec.kind == "option" for spec in self.book.instruments.values()):
            return
        from src.portfolio.exposure_limits import base_coin

        coins: dict[str, float] = {}
        for instrument, price in prices.items():
            spec = self.config.instruments.get(instrument)
            if spec is not None and (spec.kind == "perp" or base_coin(spec.symbol) not in coins):
                coins[base_coin(spec.symbol)] = float(price)
        for record in self.book.settle_expired_options(now, coins):
            realized, fee = float(record["realized"]), float(record["fee"])
            self._event("INFO", "portfolio_option_expired", f"{record['instrument']} expired at {float(record['intrinsic']):,.2f} intrinsic; realized {realized:+,.2f}",
                        {key: str(value) for key, value in record.items()}, now)
            self._tax_derivative(record["instrument"], "REALIZED_PNL", realized, now, {"kind": "option_expiry"})
            if fee:
                self._tax_derivative(record["instrument"], "TRADING_FEE", -fee, now, {"kind": "option_delivery_fee"})
            self._flush_tax(now)

    def _sleeve_failed(self, sleeve_id: str, exc: Exception, report: CycleReport, now: datetime) -> None:
        """Hold the sleeve flat this cycle; after SLEEVE_ERROR_LIMIT failures in a row, disable it until restart."""
        message = f"{type(exc).__name__}: {exc}"
        self.states[sleeve_id] = SleeveState(trade_returns=list(self.states[sleeve_id].trade_returns))
        report.sleeve_errors[sleeve_id] = message
        count = self.sleeve_error_counts[sleeve_id] = self.sleeve_error_counts.get(sleeve_id, 0) + 1
        self._event("ERROR", "portfolio_sleeve_error", f"sleeve {sleeve_id} failed ({count} in a row): {message}", {"sleeve": sleeve_id, "count": count}, now)
        if count >= SLEEVE_ERROR_LIMIT and sleeve_id not in self.disabled_sleeves:
            self.disabled_sleeves.add(sleeve_id)
            if self.notifier is not None:
                self.notifier.send_alert(event_type="portfolio_sleeve_disabled", message=f"Sleeve {sleeve_id} failed {count} cycles in a row and is held flat until restart.",
                                         metadata={"sleeve": sleeve_id, "error": message})

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
                        self._tax_derivative(event["instrument"], "FUNDING_FEE", -float(event["payment"]), now, {"kind": "funding"})
                    elif event["type"] == "liquidation":
                        self._book_liquidation(event, symbols, now)
                    events.append(event)
            elif getattr(adapter, "live", False):
                for event in adapter.on_market_update(prices={symbol: prices[instrument] for symbol, instrument in symbols.items() if instrument in prices}, timestamp=now):
                    event = {**event, "venue": venue, "instrument": symbols.get(event.get("symbol"), event.get("symbol"))}
                    self._event("ERROR", f"portfolio_{event['type']}", f"{event['instrument']} changed on the exchange without our orders: {event}", event, now)
                    if self.notifier is not None:
                        self.notifier.send_alert(event_type=event["type"], message=f"{event['instrument']} position changed on the exchange without our orders. "
                                                 "Only reductions until resolved (--portfolio-adopt-exchange).", metadata=event)
                    events.append(event)
        return events

    def _book_liquidation(self, event: dict[str, Any], symbols: Mapping[str, str], now: datetime) -> None:
        """A sandbox liquidation closed positions on the exchange; close them in the book at the same fills."""
        adapter = self.adapters[event["venue"]]
        for order in adapter.list_orders():
            if order.order_id.startswith("liquidation-") and order.symbol in event["closed"] and order.order_id not in self.book.liquidations_booked:
                instrument = symbols[order.symbol]
                realized = self.book.apply_fill(instrument, order.side, self._on_lot_grid(instrument, order.filled_size), order.fill_price, order.fee)
                self.book.liquidations_booked.append(order.order_id)
                self._tax_fill(instrument, order.side, float(order.filled_size), float(order.fill_price), float(order.fee), float(realized), order.timestamp or now, order.order_id)

    # --- tax ledger ------------------------------------------------------------------------------------------------

    def _tax_fill(self, instrument: str, side: str, units: float, price: float, fee: float, realized: float, now: datetime, order_id: str) -> None:
        """Queue the tax records one fill creates: realized P&L and the fee for a perp; the trade itself for spot (FIFO lots)."""
        if not self.record_tax:
            return
        spec = self.book.instruments.get(instrument) or self.config.instruments[instrument]
        if spec.kind in ("perp", "option"):  # derivatives: realized P&L and fees (opening an option isn't a taxable event)
            if realized:
                self._tax_derivative(instrument, "REALIZED_PNL", realized, now, {"order_id": order_id})
            if fee:
                self._tax_derivative(instrument, "TRADING_FEE", -fee, now, {"order_id": order_id})
        else:
            self.pending_tax.append({"kind": "spot_trade", "timestamp": now.isoformat(), "exchange": spec.venue, "pair": spec.symbol, "side": side,
                                     "price": price, "size": units, "fee": fee, "order_id": order_id})
        self._flush_tax(now)

    def _tax_derivative(self, instrument: str, transaction_type: str, amount: float, now: datetime, metadata: dict[str, Any]) -> None:
        if not self.record_tax or not amount:
            return
        spec = self.book.instruments.get(instrument) or self.config.instruments[instrument]
        try:
            from src.data.kraken_futures import venue_symbol_for

            venue_symbol = venue_symbol_for(spec.symbol) if spec.venue == "kraken_futures" else spec.symbol
        except ValueError:
            venue_symbol = spec.symbol
        currency = "EUR" if spec.venue == "kraken" else "USD"
        self.pending_tax.append({"kind": "derivative", "timestamp": now.isoformat(), "venue_symbol": venue_symbol, "transaction_type": transaction_type,
                                 "amount": amount, "currency": currency, "metadata": {"instrument": instrument, "portfolio": self.config.name, **metadata}})

    def _flush_tax(self, now: datetime) -> None:
        """Write queued tax records; anything that fails stays queued (and in the checkpoint) and is retried next cycle."""
        if not self.pending_tax or self.trade_logger is None:
            return
        remaining = []
        for record in self.pending_tax:
            try:
                stamp = datetime.fromisoformat(record["timestamp"])
                if record["kind"] == "derivative":
                    self.trade_logger.log_derivative_event(timestamp=stamp, venue_symbol=record["venue_symbol"], transaction_type=record["transaction_type"],
                                                           amount=record["amount"], currency=record["currency"], source="portfolio", metadata=record["metadata"])
                else:
                    _trade_id, error = self.trade_logger.log_trade(timestamp=stamp, source="portfolio_tax", exchange=record["exchange"], pair=record["pair"],
                                                                   side=record["side"], price=record["price"], size=record["size"], fee=record["fee"],
                                                                   record_tax_event=True, strategy_id="portfolio")
                    if error:  # log_trade reports a failed tax write instead of raising
                        raise RuntimeError(error)
            except Exception as exc:  # noqa: BLE001 - keep the record and retry rather than lose a tax flow
                remaining.append({**record, "last_error": f"{type(exc).__name__}: {exc}"})
        new_failures = [record for record in remaining if not record.get("alerted")]
        if new_failures:  # alert once per record, not every cycle it stays queued
            self._event("ERROR", "portfolio_tax_record_failed", f"{len(new_failures)} tax records could not be written yet; retrying each cycle",
                        {"first_error": new_failures[0]["last_error"], "queued": len(remaining)}, now)
            if self.notifier is not None:
                self.notifier.send_alert(event_type="tax_record_failed", message=f"{len(remaining)} tax records are queued and retried each cycle.",
                                         metadata={"error": new_failures[0]["last_error"]})
        self.pending_tax = [{**record, "alerted": True} for record in remaining]

    def _on_lot_grid(self, instrument: str, units: float) -> Decimal:
        """An exchange's float fill size as the exact multiple of the lot step it is (0.07840000000000001 -> 0.0784).

        Without this, float noise accumulates in the Decimal book and a fully
        closed position is left at 1e-17 instead of 0.
        """
        step = Decimal(str(self.config.instruments[instrument].lot_step)) or FALLBACK_STEP
        return (Decimal(str(units)) / step).to_integral_value() * step

    def _decide_and_trade(self, report: CycleReport, *, scales: Mapping[str, float], prices: Mapping[str, float], stale: Sequence[str], now: datetime) -> None:
        allocated = {sleeve_id: (spec.instrument, 0.0 if sleeve_id in self.disabled_sleeves else self.states[sleeve_id].weight * scales.get(sleeve_id, 0.0) * self.config.scale)
                     for sleeve_id, spec in self.sleeves.items()}
        self.book.set_sleeve_targets(allocated)
        report.targets, attribution = net_targets(allocated)
        equity = float(self.book.equity())
        report.adjusted, report.risk_actions = apply_portfolio_risk(
            report.targets, config=self.config.risk, venues=self.config.venues(), can_short=self.config.can_short(),
            current=self.book.weights(), equity=equity, peak_equity=float(self.book.peak_equity),
            day_start_equity=float(self.book.day_start_equity), stale=stale,
            groups=self.config.groups(), underlyings=self.config.underlyings(), estimate=self.risk_model.estimate(),
        )
        plan = plan_orders(self.book.units(), report.adjusted, prices=prices, equity=equity, instruments=self.config.instruments, band=self.config.rebalance_band,
                           small_lot_cap=self.config.small_lot_cap(equity))
        busy = {meta["instrument"] for meta in self.pending_orders.values()}
        cooling = self._cooldowns(now)
        orders = []
        for order in plan.orders:
            if order.instrument in busy:  # never stack a second order on one whose fill isn't settled
                plan.skipped.append(SkippedChange(order.instrument, "order_pending", self.book.weights().get(order.instrument, 0.0), order.target_weight))
            elif order.instrument in cooling and not order.reduce_only:  # repeated rejections: only reductions until the cooldown ends
                plan.skipped.append(SkippedChange(order.instrument, "rejection_cooldown", self.book.weights().get(order.instrument, 0.0), order.target_weight))
            elif self.unreconciled and not order.reduce_only:  # an unknown state: only reductions until resolved
                plan.skipped.append(SkippedChange(order.instrument, "unreconciled", self.book.weights().get(order.instrument, 0.0), order.target_weight))
            else:
                orders.append(order)
        report.orders, report.skipped = orders, plan.skipped
        for number, order in enumerate(orders):
            self._execute(order, number=number, attribution=attribution.get(order.instrument, {}), report=report, now=now)
        self._settle_pending(report, now)
        report.cooldowns = dict(self._cooldowns(now))

    # --- exchange stops ----------------------------------------------------------------------------------------------

    def _sync_stops(self, prices: Mapping[str, float], report: CycleReport, now: datetime) -> None:
        """Keep one reduce-only stop resting on each live venue for every open perp position (`[risk] exchange_stop_pct`).

        A stop is set at `exchange_stop_pct` beyond the price when the position last changed, and moved only when the
        position changes (so it caps the loss from that point; it is not a trailing stop). Instruments with an order
        still pending are left alone until it settles. The adapter talks to the exchange only when a position
        changed, on decision bars, and on the first cycle after a start.
        """
        pct = self.config.risk.exchange_stop_pct
        if pct is None:
            return
        pending = {meta["instrument"] for meta in self.pending_orders.values()}
        units = self.book.units()
        for venue, adapter in self.adapters.items():
            sync = getattr(adapter, "sync_protective_stops", None)
            if not callable(sync):
                continue
            desired: dict[str, tuple[str, float, float] | None] = {}
            changed = False
            for instrument, spec in self.config.instruments.items():
                if spec.venue != venue or spec.kind != "perp" or instrument in pending:
                    continue
                held = units.get(instrument, Decimal(0))
                anchor = self.stop_anchors.get(instrument)
                if not held:
                    changed |= self.stop_anchors.pop(instrument, None) is not None
                    desired[spec.symbol] = None
                    continue
                if anchor is None or anchor["units"] != str(held):
                    price = float(prices.get(instrument) or self.book.marks.get(instrument, 0))
                    if price <= 0:
                        continue
                    long = held > 0
                    anchor = {"units": str(held), "side": "sell" if long else "buy", "stop": price * (1 - pct) if long else price * (1 + pct),
                              "reference_price": price, "set_at": now.isoformat()}
                    self.stop_anchors[instrument] = anchor
                    changed = True
                desired[spec.symbol] = (anchor["side"], abs(float(held)), float(anchor["stop"]))
            if not (changed or report.decided or not self.stops_verified):
                continue
            try:
                actions = sync(desired, now=now)
            except Exception as exc:  # noqa: BLE001 - the positions stay; the next cycle retries
                actions = [{"action": "failed", "symbol": "*", "message": f"{type(exc).__name__}: {exc}"}]
            self.stops_verified = True
            report.stop_actions += [{**action, "venue": venue} for action in actions]
            for action in actions:
                if action["action"] in {"placed", "cancelled"}:
                    self._event("INFO", f"portfolio_exchange_stop_{action['action']}", f"exchange stop {action['action']}: {action}", action, now)
            failures = [action for action in actions if action["action"] == "failed"]
            if failures:
                self._event("ERROR", "portfolio_exchange_stop_failed", f"exchange stop not in place: {failures}", {"failures": failures}, now)
                if self.notifier is not None and not self.stop_failure_alerted:
                    self.notifier.send_alert(event_type="exchange_stop_failed", message=f"Protective stop not in place on {venue}: {failures[0]['message']}. Retrying each cycle.",
                                             metadata={"failures": failures})
                self.stop_failure_alerted = True
            elif self.stop_failure_alerted:
                self.stop_failure_alerted = False
                self._event("INFO", "portfolio_exchange_stop_restored", "exchange stops are in place again", {}, now)

    def _book_stop_fills(self, report: CycleReport, now: datetime) -> None:
        """Book fills of exchange stops that fired (even while this process was down) and keep the sleeves from buying back.

        Each fill goes through the normal path (book, tax, trade log, Telegram) once: booked fill ids are kept in the
        checkpoint. Every sleeve holding that side is closed in its own state and blocked from re-entering until its
        signal leaves that side, as after a sleeve's own stop.
        """
        if self.config.risk.exchange_stop_pct is None and not self.stop_anchors:
            return
        for venue, adapter in self.adapters.items():
            read = getattr(adapter, "protective_stop_fills", None)
            if not callable(read) or not any(self.config.instruments[i].venue == venue for i in self.stop_anchors):
                continue
            instruments = {spec.symbol: instrument for instrument, spec in self.config.instruments.items() if spec.venue == venue}
            for fill in read():
                instrument = instruments.get(fill["symbol"])
                if fill["fill_id"] in self.stop_fills_booked or instrument is None:
                    continue
                meta = {"order_id": f"stop-{fill['fill_id']}"[:100], "instrument": instrument, "side": fill["side"], "units": str(fill["size"]),
                        "price": fill["price"], "reason": "exchange_stop", "reduce_only": True, "sleeves": {}, "submitted_at": now.isoformat()}
                self._book_fill(meta, filled_size=float(fill["size"]), fill_price=float(fill["price"]), fee=float(fill["fee"]), report=report, now=now)
                self.stop_fills_booked = (self.stop_fills_booked + [fill["fill_id"]])[-500:]
                closed_side = "long" if fill["side"] == "sell" else "short"
                for sleeve_id, spec in self.sleeves.items():
                    state = self.states[sleeve_id]
                    if spec.instrument == instrument and ((state.weight > 0) if closed_side == "long" else (state.weight < 0)):
                        SleeveRunner._close_position(state, float(fill["price"]))
                        state.reentry_block = closed_side
                        self.last_decisions[sleeve_id] = {"action": "stop", "reason": "exchange_stop", "bar": now.isoformat()}
                if not self.book.units().get(instrument):
                    self.stop_anchors.pop(instrument, None)
                message = f"Exchange stop fired: {fill['side']} {fill['size']} {instrument} at {fill['price']:,.2f}; its sleeves stay out until their signal resets."
                self._event("ERROR", "portfolio_exchange_stop_filled", message, {**fill, "instrument": instrument}, now)
                if self.notifier is not None:
                    self.notifier.send_alert(event_type="exchange_stop_filled", message=message, metadata={"instrument": instrument})

    # --- baskets -------------------------------------------------------------------------------------------------------

    def _step_baskets(self, bars: BarsByKey, report: CycleReport, now: datetime) -> None:
        """Set each basket's member weights at every daily close since the last one processed.

        Off the rebalance schedule nothing changes. On a first start the members take the weights of the latest
        rebalance on or before the latest close, which is what the research backtest holds then. If the signal data
        can't be fetched, the basket keeps its weights and the same days are tried again next cycle.
        """
        from src.portfolio.basket import is_rebalance_day, weights_at

        for basket in self.config.baskets:
            if not basket.enabled or self.basket_source is None:
                continue
            members = basket.members()
            closes = sorted({bar.timestamp + timedelta(days=1) for _coin, instrument in members.values() for bar in bars.get((instrument, "1d"), [])})
            last = self.basket_last_decision.get(basket.id)
            pending = [stamp for stamp in closes if last is None or stamp > datetime.fromisoformat(last)]
            if not pending:
                continue
            if last is None:
                latest = pending[-1]
                since_epoch = (latest - datetime(1970, 1, 1, tzinfo=latest.tzinfo)).days
                pending = [latest - timedelta(days=since_epoch % basket.rebalance_days)]
            for decision in pending:
                if is_rebalance_day(decision, basket.rebalance_days):
                    try:
                        weights = weights_at(basket, self.basket_source(basket, now), decision)
                    except Exception as exc:  # noqa: BLE001 - keep the weights; retry these days next cycle
                        self._event("WARNING", "portfolio_basket_data_failed", f"{basket.id}: signal data unavailable ({type(exc).__name__}: {exc})", {}, now)
                        break
                    if weights is not None:
                        for member_id, (coin, instrument) in members.items():
                            state = self.states[member_id]
                            target = float(weights.get(coin, 0.0))
                            if target != state.weight:
                                state.entry_price = float(self.book.marks.get(instrument, 0) or 0) or None
                                state.bars_held = 0
                            state.weight, state.last_signal = target, float(np.sign(target))
                        longs = sorted(coin for coin, value in weights.items() if value > 0)
                        shorts = sorted(coin for coin, value in weights.items() if value < 0)
                        self.last_decisions[basket.id] = {"action": "rebalance", "reason": f"long {longs}, short {shorts}", "bar": decision.isoformat()}
                        self._event("INFO", "portfolio_basket_rebalanced", f"{basket.id} at {decision:%Y-%m-%d}: long {longs}, short {shorts}",
                                    {"basket": basket.id, "weights": weights}, now)
                self.basket_last_decision[basket.id] = (closes[-1] if last is None else decision).isoformat()

    def _cooldowns(self, now: datetime) -> dict[str, str]:
        """Active rejection cooldowns; expired ones are dropped (the streak stays, so one more rejection restarts it)."""
        for instrument, until in list(self.cooldown_until.items()):
            if now >= datetime.fromisoformat(until):
                del self.cooldown_until[instrument]
                self._event("INFO", "portfolio_rejection_cooldown_ended", f"{instrument}: rejection cooldown over, orders resume",
                            {"instrument": instrument}, now)
        return dict(self.cooldown_until)

    def _note_rejection(self, instrument: str, now: datetime) -> None:
        """Count a rejection; at the configured streak, start (or restart) the instrument's cooldown."""
        streak = self.rejection_streak[instrument] = self.rejection_streak.get(instrument, 0) + 1
        after = self.config.risk.rejection_cooldown_after
        if after and streak >= after and instrument not in self.cooldown_until:
            until = now + timedelta(hours=self.config.risk.rejection_cooldown_hours)
            self.cooldown_until[instrument] = until.isoformat()
            self._event("WARNING", "portfolio_rejection_cooldown", f"{instrument}: {streak} rejected orders in a row; only reductions until {until:%Y-%m-%d %H:%M} UTC",
                        {"instrument": instrument, "streak": streak, "until": until.isoformat()}, now)

    def _execute(self, order: PlannedOrder, *, number: int, attribution: Mapping[str, float], report: CycleReport, now: datetime) -> None:
        spec = self.config.instruments[order.instrument]
        adapter = self.adapters[spec.venue]
        order_id = f"pf-{self.cycle}-{number}-{spec.venue}-{spec.symbol}"
        extra = {"reduce_only": order.reduce_only} if _accepts(adapter.submit_order, "reduce_only") else {}
        result = adapter.submit_order(order_id=order_id, side=order.side, size=float(order.units), price=order.price, timestamp=now, symbol=spec.symbol, **extra)
        meta = {"order_id": order_id, "instrument": order.instrument, "side": order.side, "units": str(order.units), "price": order.price,
                "reason": order.reason, "reduce_only": order.reduce_only, "sleeves": dict(attribution), "submitted_at": now.isoformat()}
        if result.status == "SUBMITTED":  # a live exchange: the fill arrives later (settle_orders)
            self.pending_orders[order_id] = meta
            self._event("INFO", "portfolio_order_submitted", f"{order.side} {order.units} {order.instrument} sent ({order.reason})", meta, now)
            return
        if result.status != "FILLED" or not result.filled_size:
            rejection = {"order_id": order_id, "instrument": order.instrument, "side": order.side, "units": str(order.units), "reason": order.reason, "message": result.message}
            report.rejected.append(rejection)
            self._event("WARNING", "portfolio_order_rejected", f"{order.side} {order.units} {order.instrument} rejected: {result.message}", rejection, now)
            self._note_rejection(order.instrument, now)
            return
        self._book_fill(meta, filled_size=float(result.filled_size), fill_price=float(result.fill_price), fee=float(result.fee), report=report, now=now)

    def _book_fill(self, meta: Mapping[str, Any], *, filled_size: float, fill_price: float, fee: float, report: CycleReport, now: datetime) -> None:
        """Book one fill (immediate or settled later): the book, the tax ledger, the trade log, the event log and Telegram."""
        instrument = meta["instrument"]
        self.rejection_streak.pop(instrument, None)  # the exchange takes orders again
        spec = self.config.instruments[instrument]
        drivers = dict(meta.get("sleeves", {}))
        strategy_id = next(iter(drivers)) if len(drivers) == 1 else "portfolio"
        realized = self.book.apply_fill(instrument, meta["side"], self._on_lot_grid(instrument, filled_size), fill_price, fee)
        self._tax_fill(instrument, meta["side"], filled_size, fill_price, fee, float(realized), now, meta["order_id"])
        fill = {"order_id": meta["order_id"], "instrument": instrument, "side": meta["side"], "units": filled_size, "price": fill_price,
                "fee": fee, "reason": meta["reason"], "reduce_only": meta["reduce_only"], "strategy_id": strategy_id, "sleeves": drivers}
        report.fills.append(fill)
        if self.trade_logger is not None:
            self.trade_logger.log_trade(timestamp=now, source="portfolio", exchange=spec.venue, pair=spec.symbol, side=meta["side"], price=fill_price,
                                        size=filled_size, fee=fee, strategy_id=strategy_id)
            self._event("INFO", "portfolio_fill", f"{meta['side']} {filled_size} {instrument} @ {fill_price} ({meta['reason']})", fill, now)
        if self.notifier is not None:
            self.notifier.send_trade_alert(self._trade_alert(meta, fill, report, now))

    def _settle_pending(self, report: CycleReport, now: datetime) -> None:
        """Book fills of orders a live exchange accepted earlier, drop the ones that ended, and book late fills."""
        # Stop watching a written-off order once it is both an hour old and was checked for several cycles, so a long
        # runtime interval (or a fast replay) can't drop it before its late fill had a chance to appear
        horizon = (now - LATE_FILL_WATCH).isoformat()
        for meta in self.written_off_orders.values():
            meta["checks"] = int(meta.get("checks", 0)) + 1
        self.written_off_orders = {order_id: meta for order_id, meta in self.written_off_orders.items()
                                   if meta.get("written_off_at", "") >= horizon or meta["checks"] <= LATE_FILL_MIN_CHECKS}
        if not self.pending_orders and not self.written_off_orders:
            return
        for venue, adapter in self.adapters.items():
            settle = getattr(adapter, "settle_orders", None)
            if not callable(settle):
                continue
            for item in settle():
                # One of ours written off earlier (possibly by the adapter of a previous process): only a fill matters
                late = item["order_id"] in self.written_off_orders
                if late and not item.get("filled_size"):
                    continue
                meta = self.written_off_orders.pop(item["order_id"]) if late else self.pending_orders.get(item["order_id"])
                if meta is None:
                    continue
                if item.get("filled_size"):
                    self._book_fill(meta, filled_size=float(item["filled_size"]), fill_price=float(item["fill_price"]), fee=float(item["fee"]), report=report, now=now)
                if late:
                    message = f"late fill: {meta['side']} {item['filled_size']} {meta['instrument']} at {float(item['fill_price']):,.2f} appeared after the order was written off"
                    self._event("WARNING", "portfolio_late_fill", message, {**meta, "filled_size": item["filled_size"]}, now)
                    if self.notifier is not None:
                        self.notifier.send_alert(event_type="late_fill", message=f"{message}; booked now.", metadata={})
                    continue
                if item.get("status") in {"FILLED", "CANCELED"}:
                    self.pending_orders.pop(item["order_id"], None)
                    if item["status"] == "CANCELED" and not item.get("filled_size"):
                        self._event("WARNING", "portfolio_order_unfilled", f"{meta['side']} {meta['units']} {meta['instrument']} ended without a fill", meta, now)
                        self.written_off_orders[item["order_id"]] = {**meta, "written_off_at": now.isoformat()}

    def _trade_alert(self, meta: Mapping[str, Any], fill: Mapping[str, Any], report: CycleReport, now: datetime) -> TradeAlert:
        notes = []
        for sleeve_id, weight in fill["sleeves"].items():
            decision = report.sleeve_decisions.get(sleeve_id)
            action = f", {decision.action}" + (f" ({decision.reason})" if decision.reason else "") if decision else ""
            notes.append(f"Sleeve {sleeve_id} ({self.sleeves[sleeve_id].strategy}): target {weight:+.1%} of equity{action}")
        for action in report.risk_actions:
            if action.instrument == meta["instrument"]:
                notes.append(f"Risk {action.rule}: {action.before:+.1%} -> {action.after:+.1%}")
        position = self.book.positions.get(meta["instrument"])
        equity = float(self.book.equity())
        peak = float(self.book.peak_equity)
        return TradeAlert(
            mode=self.mode, strategy_name=f"portfolio {self.config.name}", side=meta["side"], size=float(fill["units"]), price=float(fill["price"]),
            symbol=meta["instrument"], intent=meta["reason"], fee=float(fill["fee"]), position_size=float(position.units) if position else 0.0,
            avg_entry_price=float(position.avg_entry) if position and position.units else None, equity=equity,
            pnl=equity - float(self.book.initial_equity), drawdown_pct=max(0.0, 1.0 - equity / peak) if peak > 0 else 0.0, timestamp=now, notes=tuple(notes),
        )

    def _book_live_funding(self, now: datetime) -> None:
        """Book each funding hour since the last one for positions on a live exchange, from Kraken's published hourly rates.

        Payment = units held x mark x hourly rate (longs pay when the rate is
        positive). The mark is the latest price, and the units are what the
        book holds, which only change at decisions. The records are flagged
        as estimates from public rates; `_check_equity_drift` catches
        anything this misses.
        """
        live = {venue for venue, adapter in self.adapters.items() if getattr(adapter, "live", False)}
        if not live:
            return
        from src.data.kraken_futures import fetch_funding_history, venue_symbol_for

        source = self.funding_source or fetch_funding_history
        for instrument, units in self.book.units().items():
            spec = self.config.instruments[instrument]
            if spec.venue not in live or spec.kind != "perp":
                continue
            since = self.funding_booked_until.get(instrument)
            if since is None:  # first time we hold it: book from now on
                self.funding_booked_until[instrument] = now.isoformat()
                continue
            try:
                rates = [rate for rate in source(venue_symbol_for(spec.symbol)) if datetime.fromisoformat(since) < rate.timestamp <= now]
            except Exception as exc:  # noqa: BLE001 - try again next cycle
                self._event("WARNING", "portfolio_funding_fetch_failed", f"funding rates for {instrument}: {exc}", {}, now)
                continue
            mark = self.book.marks.get(instrument)
            for rate in sorted(rates, key=lambda item: item.timestamp):
                payment = float(units) * float(mark) * rate.hourly_rate
                self.book.book_funding(instrument, payment)
                self.funding_estimated[instrument] = self.funding_estimated.get(instrument, 0.0) - payment  # trued up from the account log
                self._tax_derivative(instrument, "FUNDING_FEE", -payment, rate.timestamp, {"kind": "funding", "estimate": "public hourly rate x latest mark"})
                self.funding_booked_until[instrument] = rate.timestamp.isoformat()
        for instrument in [key for key in self.funding_booked_until if key not in self.book.units()]:
            self.funding_booked_until.pop(instrument)

    def _start_account_log(self, now: datetime) -> None:
        """On a live venue's first cycle, note where its account log ends: entries from before this book aren't ours."""
        for venue, adapter in self.adapters.items():
            read = getattr(adapter, "account_log", None)
            if not getattr(adapter, "live", False) or not callable(read) or venue in self.account_log_last_id:
                continue
            try:
                latest = read(after_id=None)
                unrealized = adapter.read_unrealized_funding()
            except Exception as exc:  # noqa: BLE001 - try again next cycle
                self._event("WARNING", "portfolio_account_log_failed", f"{venue}: account log unreadable ({type(exc).__name__}: {exc})", {}, now)
                continue
            self.account_log_last_id[venue] = int(latest[0]["id"]) if latest else 0
            for instrument, spec in self.config.instruments.items():
                if spec.venue == venue and spec.kind == "perp":
                    self.funding_baseline[instrument] = float(unrealized.get(spec.symbol, 0.0))
            self._event("INFO", "portfolio_account_log_start", f"{venue}: account-log reconciliation starts after entry {self.account_log_last_id[venue]}",
                        {"venue": venue}, now)

    def _reconcile_account_log(self, report: CycleReport, now: datetime) -> None:
        """Correct estimated fees and funding with Kraken's account log, on decision bars (live venues only).

        - **Fees:** each fill this book sent (its client id carries the book's prefix) is estimated at the taker rate
          when it is booked. The log's fee entry for that fill (matched by fill id = `execution`) replaces it: the gap is
          booked as a fee in the book and a TRADING_FEE record dated at the fill. Fills of other senders (the kill
          switch, manual trades) keep their own records and aren't touched.
        - **Funding** belongs to the position, not to a fill, and Kraken realizes it at fills (`realized_funding`).
          So it is trued up on totals per instrument: realized (full precision from /fills, else the log's rounded
          figure) plus Kraken's current unrealized funding,
          minus what the book estimated from public rates and what was trued up before. The gap is booked as funding
          and a FUNDING_FEE record dated now.
        - The first run starts from the log's latest entry, so trades from before the book existed aren't re-booked.
        """
        for venue, adapter in self.adapters.items():
            read = getattr(adapter, "account_log", None)
            if not getattr(adapter, "live", False) or not callable(read):
                continue
            instruments = {spec.symbol: instrument for instrument, spec in self.config.instruments.items() if spec.venue == venue and spec.kind == "perp"}
            contracts = {adapter.contracts[symbol].venue_symbol.lower(): instrument for symbol, instrument in instruments.items() if symbol in adapter.contracts}
            last = self.account_log_last_id.get(venue)
            if last is None:
                continue  # not started yet (`_start_account_log` retries each cycle)
            try:
                entries = read(after_id=last)
                fills = adapter.recent_fills() if any(entry.get("info") == "futures trade" for entry in entries) else {}
                unrealized = adapter.read_unrealized_funding()  # now: fills this cycle already realized part of it
            except Exception as exc:  # noqa: BLE001 - estimates stay in place; the next decision bar tries again
                self._event("WARNING", "portfolio_account_log_failed", f"{venue}: account log unreadable ({type(exc).__name__}: {exc})", {}, now)
                continue
            sizes = {entry.get("execution"): abs(float(entry.get("new_balance") or 0.0) - float(entry.get("old_balance") or 0.0))
                     for entry in entries if entry.get("info") == "futures trade" and str(entry.get("asset", "")).lower() == str(entry.get("contract", "")).lower()}
            fee_gap, funding_seen, unknown = 0.0, 0.0, []
            prefix = f"{adapter.client_id_prefix}-"
            for entry in entries:
                instrument = contracts.get(str(entry.get("contract") or "").lower())
                if instrument is None or str(entry.get("asset", "")).lower() == str(entry.get("contract", "")).lower():
                    continue  # not our contract, or the position entry (the money is on the currency entry)
                execution = str(entry.get("execution") or "")
                fill = fills.get(execution) if execution else None
                realized_value = fill.get("realized_funding") if fill is not None and fill.get("realized_funding") is not None else entry.get("realized_funding")
                if realized_value is not None:  # full precision from /fills when Kraken still lists the fill
                    realized = float(realized_value)
                    self.funding_actual[instrument] = self.funding_actual.get(instrument, 0.0) + realized
                    funding_seen += realized
                if entry.get("info") != "futures trade" or entry.get("fee") is None:
                    continue
                owner = str(fill.get("cliOrdId") or "") if fill is not None else None
                if owner is None:
                    unknown.append(execution)
                    continue
                if not owner.startswith(prefix) or execution not in sizes:
                    continue
                spec = self.config.instruments[instrument]
                estimated = sizes[execution] * float(entry.get("trade_price") or 0.0) * adapter.contracts[spec.symbol].taker_fee_rate
                gap = float(entry["fee"]) - estimated
                if abs(gap) > 1e-12:
                    stamp = datetime.fromisoformat(str(entry["date"]).replace("Z", "+00:00"))
                    self.book.book_fee(instrument, gap)
                    self._tax_derivative(instrument, "TRADING_FEE", -gap, stamp, {"kind": "fee_true_up", "execution": execution, "log_id": entry.get("id"),
                                                                                  "actual": float(entry["fee"]), "estimated": estimated})
                    fee_gap += gap
            if entries:
                self.account_log_last_id[venue] = max(int(entry["id"]) for entry in entries)
            if unknown:
                self.unknown_fee_fills = (self.unknown_fee_fills + unknown)[-200:]
                self._event("WARNING", "portfolio_account_log_unmatched", f"{venue}: {len(unknown)} trade fees whose fill is no longer in /fills; left as estimated",
                            {"executions": unknown[:20]}, now)
            funding_gap = 0.0
            for symbol, instrument in instruments.items():
                actual = self.funding_actual.get(instrument, 0.0) + float(unrealized.get(symbol, 0.0)) - self.funding_baseline.get(instrument, 0.0)
                gap = actual - self.funding_estimated.get(instrument, 0.0) - self.funding_trued_up.get(instrument, 0.0)
                if abs(gap) > 1e-9:
                    self.book.book_funding(instrument, -gap)  # book_funding takes "paid"
                    self._tax_derivative(instrument, "FUNDING_FEE", gap, now, {"kind": "funding_true_up", "actual_received_total": actual,
                                                                                "estimated_received_total": self.funding_estimated.get(instrument, 0.0)})
                    self.funding_trued_up[instrument] = self.funding_trued_up.get(instrument, 0.0) + gap
                    funding_gap += gap
            report.account_log[venue] = {"entries": len(entries), "fee_gap": fee_gap, "funding_realized": funding_seen, "funding_gap": funding_gap, "unmatched": len(unknown)}
            if fee_gap or funding_gap:
                self._event("INFO", "portfolio_account_log_true_up", f"{venue}: fees {fee_gap:+.6f}, funding {funding_gap:+.6f} from Kraken's account log",
                            report.account_log[venue], now)

    def _check_equity_drift(self, now: datetime) -> None:
        """Compare the book's equity with a live exchange's margin equity; alert once if they drift apart.

        Funding, fees and prices are estimated in the book, so small
        differences are expected. More than 0.5% (or 5 in the base currency)
        means something the book doesn't know about.
        """
        for adapter in self.adapters.values():
            exchange = getattr(adapter, "margin_equity", None)
            if not getattr(adapter, "live", False) or exchange is None:
                continue
            book = float(self.book.venue_equity(adapter.exchange_name))
            drift = book - float(exchange)
            if abs(drift) > max(5.0, 0.005 * abs(float(exchange))):
                if not self.equity_drift_alerted:
                    self.equity_drift_alerted = True
                    self._event("WARNING", "portfolio_equity_drift", f"book equity {book:,.2f} vs exchange {float(exchange):,.2f} ({drift:+,.2f})",
                                {"book": book, "exchange": float(exchange)}, now)
                    if self.notifier is not None:
                        self.notifier.send_alert(event_type="equity_drift", message=f"Book equity {book:,.2f} differs from the exchange's {float(exchange):,.2f} by {drift:+,.2f}.",
                                                 metadata={})
            else:
                self.equity_drift_alerted = False

    def adopt_exchange_state(self, *, now: datetime, reason: str) -> dict[str, Any]:
        """Make the book match each live exchange: its positions, entry prices and collateral. Logged, with the before and after.

        Used when a live portfolio starts without a checkpoint, and by the
        operator (`--portfolio-adopt-exchange`) after an external change,
        such as a manual trade or a liquidation, left the book unreconciled.
        """
        changes: dict[str, Any] = {}
        for venue, adapter in self.adapters.items():
            if not getattr(adapter, "live", False):
                continue
            state = adapter.sync_account()
            before = {instrument: float(units) for instrument, units in self.book.units().items() if self.config.instruments[instrument].venue == venue}
            for instrument, spec in self.config.instruments.items():
                if spec.venue != venue:
                    continue
                size = float(state["positions"].get(spec.symbol, 0.0))
                position = self.book.positions.setdefault(instrument, BookPosition())
                position.units = self._on_lot_grid(instrument, size) if size else Decimal(0)
                position.avg_entry = Decimal(str(state["entries"].get(spec.symbol, 0.0))) if size else Decimal(0)
            self.book.cash[venue] = Decimal(str(adapter.margin_equity - adapter.total_unrealized))
            changes[venue] = {"before": before, "after": state["positions"], "equity": state["equity"], "foreign_positions": state["foreign_positions"]}
        if self.book.initial_equity == 0 or not self.restored:
            self.book.initial_equity = self.book.peak_equity = self.book.day_start_equity = self.book.equity()
        self.unreconciled = {}
        self._event("WARNING", "portfolio_adopted_exchange_state", f"book set to the exchange's state: {reason}", changes, now)
        self._save()
        return changes

    def flatten(self, *, now: datetime, reason: str) -> CycleReport:
        """Close every position on every venue with reduce-only orders at the latest marks (the kill switch path)."""
        self.cycle += 1
        report = CycleReport(timestamp=now, decided=True, equity=0.0)
        for adapter in self.adapters.values():
            cancel_all = getattr(adapter, "cancel_all_orders", None)
            if getattr(adapter, "live", False) and callable(cancel_all):
                try:
                    cancel_all()
                except Exception as exc:  # noqa: BLE001 - still try to close the positions
                    self._event("ERROR", "portfolio_cancel_all_failed", str(exc), {}, now)
        prices = {instrument: float(price) for instrument, price in self.book.marks.items()}
        plan = plan_orders(self.book.units(), {}, prices=prices, equity=float(self.book.equity()), instruments=self.config.instruments, band=0.0)
        report.orders, report.skipped = plan.orders, plan.skipped
        for number, order in enumerate(plan.orders):
            self._execute(order, number=number, attribution={}, report=report, now=now)
        for _attempt in range(3):  # live IOC fills show up in /fills within a moment
            self._settle_pending(report, now)
            if not self.pending_orders or not any(getattr(adapter, "live", False) for adapter in self.adapters.values()):
                break
            time.sleep(1.0)
        report.mismatches = self.reconcile()
        report.equity = float(self.book.equity())
        self._event("WARNING", "portfolio_flattened", f"flattened {len(report.fills)} positions: {reason}", {"reason": reason, "fills": report.fills, "rejected": report.rejected}, now)
        self._save()
        return report

    def snapshot(self, report: CycleReport | None = None) -> dict[str, Any]:
        """What the book holds and why, for the dashboard: instruments, sleeves, risk usage and the last decision."""
        equity = float(self.book.equity())
        weights = self.book.weights()
        attribution = self.book.attribution()
        adjusted = report.adjusted if report is not None and report.decided else {}
        instruments = {}
        for instrument in sorted(self.config.instruments):
            position = self.book.positions.get(instrument)
            instruments[instrument] = {
                "target": adjusted.get(instrument), "weight": weights.get(instrument, 0.0),
                "units": float(position.units) if position else 0.0, "price": float(self.book.marks.get(instrument, 0)),
                "realized_pnl": float(position.realized_pnl) if position else 0.0, "fees": float(position.fees) if position else 0.0,
                "funding": float(position.funding) if position else 0.0,
                "exchange_stop": self.stop_anchors.get(instrument, {}).get("stop"),
            }
        sleeves = {}
        for sleeve_id, spec in self.sleeves.items():
            virtual = self.book.sleeves.get(sleeve_id)
            price = float(self.book.marks.get(spec.instrument, 0))
            decision = self.last_decisions.get(sleeve_id, {})
            sleeves[sleeve_id] = {
                "instrument": spec.instrument, "strategy": spec.strategy, "own_weight": self.states[sleeve_id].weight,
                "allocated_weight": float(virtual.units) * price / equity if virtual and equity > 0 else 0.0,
                "pnl": float(attribution.get(sleeve_id, 0)), "last_action": decision.get("action"), "last_reason": decision.get("reason"),
                "last_action_bar": decision.get("bar"), "disabled": sleeve_id in self.disabled_sleeves,
            }
        gross = sum(abs(weight) for weight in weights.values())
        peak = float(self.book.peak_equity)
        risk = self.config.risk
        exposure = exposure_summary(weights, config=self.config, estimate=self.risk_model.estimate(), units=self.groups,
                                    sleeve_weights={sleeve_id: (row["instrument"], row["allocated_weight"]) for sleeve_id, row in sleeves.items()})
        return {
            "portfolio": self.config.name, "cycle": self.cycle, "equity": equity, "initial_equity": float(self.book.initial_equity),
            "peak_equity": peak, "drawdown": max(0.0, 1.0 - equity / peak) if peak > 0 else 0.0,
            "day_start_equity": float(self.book.day_start_equity), "gross": gross, "net": sum(weights.values()),
            "limits": {"max_gross_exposure": risk.max_gross_exposure, "max_net_exposure": risk.max_net_exposure,
                       "max_instrument_weight": risk.max_instrument_weight, "max_drawdown": risk.max_drawdown, "daily_loss_limit": risk.daily_loss_limit,
                       "groups": risk.groups, "max_beta_exposure": risk.max_beta_exposure, "max_portfolio_vol": risk.max_portfolio_vol,
                       "exposure": risk.exposure, "max_average_correlation": risk.max_average_correlation, "min_effective_bets": risk.min_effective_bets},
            "exposure": exposure, "strategy_correlation": self.unit_returns.summary(), "exposure_breaches": self.exposure_breaches(equity),
            "instruments": instruments, "sleeves": sleeves, "residual_pnl": float(attribution.get("residual", 0)),
            "risk_actions": [vars_of(action) for action in (report.risk_actions if report is not None else [])],
            "fills": len(report.fills) if report is not None else 0,
        }

    def exposure_breaches(self, equity: float) -> list[dict[str, Any]]:
        """`[risk.exposure]` limits the book as held breaks, options included (greeks by full revaluation).

        The overlay keeps perp and spot targets inside the delta and scenario limits. Option positions aren't sized
        by it, and a book can drift between decisions, so the held book is checked here and reported.
        """
        if not self.config.risk.exposure or equity <= 0:
            return []
        from src.portfolio.exposure import MarketState
        from src.portfolio.exposure_limits import base_coin, book_positions, check_exposure

        spot: dict[str, float] = {}
        for instrument, spec in self.book.instruments.items():
            mark = float(self.book.marks.get(instrument, 0) or 0)
            if spec.kind != "option" and mark > 0 and (spec.kind == "perp" or base_coin(spec.symbol) not in spot):
                spot[base_coin(spec.symbol)] = mark
        try:
            positions = [position for position in book_positions(self.book.units(), {instrument: spec.kind for instrument, spec in self.book.instruments.items()})
                         if position.underlying in spot]
            when = self.last_grid_bar + self.grid_step if self.last_grid_bar else datetime.now().astimezone()
            _table, _grid, breaches = check_exposure(positions, MarketState(now=when, spot=spot), self.config.risk.exposure, equity)
        except Exception:  # noqa: BLE001 - a report must not stop the book
            return []
        return [vars_of(breach) for breach in breaches]

    # --- reconciliation, logging, checkpoints ---------------------------------------------------------------------

    def reconcile(self) -> dict[str, dict[str, float]]:
        """Book units vs each adapter's position per instrument; returns the mismatches (empty when they agree)."""
        mismatches = {}
        book_units = self.book.units()
        for instrument, spec in self.config.instruments.items():
            adapter = self.adapters[spec.venue]
            if any(meta["instrument"] == instrument for meta in self.pending_orders.values()):
                continue  # a fill is in flight: compare once it has settled
            if isinstance(adapter, SandboxCrossMarginPerpAdapter) or getattr(adapter, "live", False):
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
            "risk_model": self.risk_model.to_dict(),
            "unit_returns": self.unit_returns.to_dict(),
            "held_weights": self.held_weights,
            "last_sleeve_bar": {sleeve_id: stamp.isoformat() for sleeve_id, stamp in self.last_sleeve_bar.items()},
            "last_grid_bar": self.last_grid_bar.isoformat() if self.last_grid_bar else None,
            "last_grid_close": self.last_grid_close,
            "disabled_sleeves": sorted(self.disabled_sleeves),
            "rejection_streak": self.rejection_streak,
            "cooldown_until": self.cooldown_until,
            "basket_last_decision": self.basket_last_decision,
            "stop_anchors": self.stop_anchors,
            "stop_fills_booked": self.stop_fills_booked,
            "account_log_last_id": self.account_log_last_id,
            "funding_actual": self.funding_actual,
            "funding_estimated": self.funding_estimated,
            "funding_baseline": self.funding_baseline,
            "funding_trued_up": self.funding_trued_up,
            "unknown_fee_fills": self.unknown_fee_fills,
            "last_decisions": self.last_decisions,
            "pending_tax": self.pending_tax,
            "pending_orders": self.pending_orders,
            "written_off_orders": self.written_off_orders,
            "unreconciled": self.unreconciled,
            "funding_booked_until": self.funding_booked_until,
            "book_id": self.book_id,
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
        # Checkpoints written before the risk model existed have none: it then warms up again from the next bars
        if payload.get("risk_model") and tuple(payload["risk_model"]["instruments"]) == self.risk_model.instruments:
            restored = RiskModel.from_dict(payload["risk_model"])
            restored_settings, settings = restored.to_dict(), self.risk_model.to_dict()
            if all(restored_settings[key] == settings[key] for key in ("halflife_bars", "min_bars", "shrinkage", "prior_correlation", "stress_correlation")):
                self.risk_model = restored
        if payload.get("unit_returns") and tuple(payload["unit_returns"]["names"]) == self.unit_returns.names:
            self.unit_returns.rows = ReturnWindow.from_dict(payload["unit_returns"]).rows[-self.unit_returns.length:]
        self.held_weights = {sleeve_id: float(value) for sleeve_id, value in dict(payload.get("held_weights", {})).items()}
        self.last_sleeve_bar = {sleeve_id: datetime.fromisoformat(stamp) for sleeve_id, stamp in payload["last_sleeve_bar"].items()}
        self.last_grid_bar = datetime.fromisoformat(payload["last_grid_bar"]) if payload.get("last_grid_bar") else None
        self.last_grid_close = {instrument: float(close) for instrument, close in payload["last_grid_close"].items()}
        self.last_decisions = dict(payload.get("last_decisions", {}))
        self.pending_tax = list(payload.get("pending_tax", []))
        self.pending_orders = dict(payload.get("pending_orders", {}))
        self.written_off_orders = dict(payload.get("written_off_orders", {}))
        self.unreconciled = dict(payload.get("unreconciled", {}))
        self.funding_booked_until = dict(payload.get("funding_booked_until", {}))
        self.rejection_streak = {key: int(value) for key, value in dict(payload.get("rejection_streak", {})).items()}
        self.cooldown_until = dict(payload.get("cooldown_until", {}))
        self.basket_last_decision = dict(payload.get("basket_last_decision", {}))
        self.stop_anchors = dict(payload.get("stop_anchors", {}))
        self.stop_fills_booked = list(payload.get("stop_fills_booked", []))
        self.account_log_last_id = {venue: int(value) for venue, value in dict(payload.get("account_log_last_id", {})).items()}
        for name in ("funding_actual", "funding_estimated", "funding_baseline", "funding_trued_up"):
            setattr(self, name, {key: float(value) for key, value in dict(payload.get(name, {})).items()})
        self.unknown_fee_fills = list(payload.get("unknown_fee_fills", []))
        self.book_id = str(payload.get("book_id", self.book_id))
        return True

    def reset_peak(self, *, now: datetime) -> float:
        """Restart the drawdown count from today's equity, e.g. after the max-drawdown kill; returns the old peak.

        The kill never lifts by itself (a flat book can't recover its drawdown),
        so re-arming the book is an explicit, logged operator action.
        """
        old = float(self.book.peak_equity)
        self.book.peak_equity = self.book.equity()
        self._event("WARNING", "portfolio_peak_reset", f"equity peak reset from {old:,.2f} to {float(self.book.peak_equity):,.2f}", {"old_peak": old}, now)
        self._save()
        return old


def vars_of(item: Any) -> dict[str, Any]:
    """A slotted dataclass as a plain dict (for event metadata)."""
    return {name: getattr(item, name) for name in item.__slots__}
