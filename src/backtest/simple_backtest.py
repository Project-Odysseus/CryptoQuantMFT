"""A lightweight, strategy-driven backtester for OHLC-like market data."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.backtest.analytics import PerformanceMetrics, build_performance_metrics
from src.backtest.costs import CostModel
from src.risk.controls import RiskDecision, RiskManager, gate_reentry


StrategyFn = Callable[[Sequence[Any], int, Any], float | int | str | None]


@dataclass(slots=True)
class TradeRecord:
    """A single executed trade from the backtest loop."""

    timestamp: datetime | None
    side: str
    entry_price: float
    exit_price: float
    size: float
    return_pct: float
    equity_after_trade: float
    cost: float = 0.0
    reason: str | None = None


@dataclass(slots=True)
class BacktestResult:
    """Simple performance summary for a single strategy run."""

    total_return: float
    win_rate: float
    max_drawdown: float
    trades: int
    final_equity: float
    equity_series: list[float] = field(default_factory=list)
    timestamps: list[datetime] = field(default_factory=list)
    trade_prices: list[float] = field(default_factory=list)
    trade_timestamps: list[datetime] = field(default_factory=list)
    trade_returns: list[float] = field(default_factory=list)
    trade_sizes: list[float] = field(default_factory=list)
    trade_records: list[TradeRecord] = field(default_factory=list)
    trade_costs: list[float] = field(default_factory=list)
    metrics: PerformanceMetrics = field(default_factory=PerformanceMetrics)
    # `equity_series` only moves when a trade closes. These two are marked to
    # market every bar (one entry per input bar), which is what Sharpe,
    # drawdown and period-by-period returns should be computed from.
    mtm_equity_series: list[float] = field(default_factory=list)
    position_series: list[float] = field(default_factory=list)


class SimpleBacktester:
    """Run a user-supplied strategy over any OHLC-like series."""

    def __init__(
        self,
        strategy: StrategyFn | None = None,
        *,
        initial_equity: float = 100.0,
        threshold: float = 0.02,
        cost_model: CostModel | None = None,
        risk_manager: RiskManager | None = None,
    ) -> None:
        """Initialize the object with its runtime state."""
        self.strategy = strategy or self._default_strategy
        if initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        if threshold <= 0:
            raise ValueError("threshold must be positive")

        self.initial_equity = initial_equity
        self.threshold = threshold
        self.cost_model = cost_model
        self.risk_manager = risk_manager

    def run(self, bars: Sequence[Any]) -> BacktestResult:
        """Backtest a strategy over a sequence of OHLC-like bars."""
        if len(bars) < 2:
            raise ValueError("At least two bars are required")

        equity = self.initial_equity
        peak_equity = equity
        trade_count = 0
        wins = 0
        equity_series: list[float] = [equity]
        mtm_equity_series: list[float] = [equity]
        position_series: list[float] = [0.0]
        timestamps: list[datetime] = [_get_timestamp(bars[0])]
        trade_prices: list[float] = []
        trade_timestamps: list[datetime] = []
        trade_returns: list[float] = []
        trade_sizes: list[float] = []
        trade_records: list[TradeRecord] = []
        trade_costs: list[float] = []

        entry_price: float | None = None
        entry_timestamp: datetime | None = None
        current_position = 0.0
        entry_size = 1.0

        cost_model = self.cost_model
        bars_held = 0
        reentry_block: str | None = None

        # Strategies that can compute every bar's signal in one causal pass skip the per-bar calls, and without a
        # risk manager nothing else needs the growing history list, so long backtests stay linear in the bar count.
        signal_series = getattr(self.strategy, "signal_series", None)
        precomputed = list(signal_series(bars)) if callable(signal_series) else None
        needs_history = precomputed is None or self.risk_manager is not None

        for index in range(1, len(bars)):
            history = list(bars[: index + 1]) if needs_history else []
            current_bar = bars[index]
            signal_value = precomputed[index] if precomputed is not None else self.strategy(history, index, current_bar)
            signal, reentry_block, _blocked = gate_reentry(_normalize_signal(signal_value), reentry_block)
            close_price = _get_close(current_bar)
            timestamp = _get_timestamp(current_bar)

            bars_held = bars_held + 1 if current_position != 0.0 else 0

            forced_exit_reason: str | None = None
            if self.risk_manager is not None and current_position != 0.0 and entry_price is not None:
                exit_decision = self.risk_manager.evaluate_exit(
                    bars=history,
                    current_bar=current_bar,
                    position_side="long" if current_position > 0.0 else "short",
                    avg_entry_price=entry_price,
                    bars_held=bars_held,
                )
                if exit_decision.force_exit:
                    forced_exit_reason = exit_decision.reason

            if index == len(bars) - 1 and current_position != 0.0:
                if current_position > 0.0:
                    exit_price = self._apply_cost(close_price, side="sell", cost_model=cost_model)
                    return_pct = (exit_price - entry_price) / entry_price if entry_price is not None else 0.0
                    trade_count += 1
                    if return_pct > 0:
                        wins += 1
                    equity *= 1 + entry_size * return_pct
                    trade_prices.append(exit_price)
                    trade_timestamps.append(timestamp)
                    trade_returns.append(return_pct)
                    trade_sizes.append(entry_size)
                    trade_costs.append(abs(exit_price - close_price) + abs(entry_price - close_price) if entry_price is not None else 0.0)
                    trade_records.append(
                        TradeRecord(
                            timestamp=timestamp,
                            side="long",
                            entry_price=entry_price if entry_price is not None else close_price,
                            exit_price=exit_price,
                            size=entry_size,
                            return_pct=return_pct,
                            equity_after_trade=equity,
                            cost=abs(exit_price - close_price) + abs(entry_price - close_price) if entry_price is not None else 0.0,
                        )
                    )
                else:
                    exit_price = self._apply_cost(close_price, side="buy", cost_model=cost_model)
                    return_pct = (entry_price - exit_price) / entry_price if entry_price is not None else 0.0
                    trade_count += 1
                    if return_pct > 0:
                        wins += 1
                    equity *= 1 + entry_size * return_pct
                    trade_prices.append(exit_price)
                    trade_timestamps.append(timestamp)
                    trade_returns.append(return_pct)
                    trade_sizes.append(entry_size)
                    trade_costs.append(abs(exit_price - close_price) + abs(entry_price - close_price) if entry_price is not None else 0.0)
                    trade_records.append(
                        TradeRecord(
                            timestamp=timestamp,
                            side="short",
                            entry_price=entry_price if entry_price is not None else close_price,
                            exit_price=exit_price,
                            size=entry_size,
                            return_pct=return_pct,
                            equity_after_trade=equity,
                            cost=abs(exit_price - close_price) + abs(entry_price - close_price) if entry_price is not None else 0.0,
                        )
                    )
                current_position = 0.0
                entry_price = None
                entry_timestamp = None
            elif forced_exit_reason is not None:
                exit_side = "sell" if current_position > 0.0 else "buy"
                reentry_block = "long" if current_position > 0.0 else "short"
                exit_price = self._apply_cost(close_price, side=exit_side, cost_model=cost_model)
                if current_position > 0.0:
                    return_pct = (exit_price - entry_price) / entry_price
                else:
                    return_pct = (entry_price - exit_price) / entry_price
                trade_count += 1
                if return_pct > 0:
                    wins += 1
                equity *= 1 + entry_size * return_pct
                trade_prices.append(exit_price)
                trade_timestamps.append(timestamp)
                trade_returns.append(return_pct)
                trade_sizes.append(entry_size)
                trade_costs.append(abs(exit_price - close_price) + abs(entry_price - close_price))
                trade_records.append(
                    TradeRecord(
                        timestamp=timestamp,
                        side="long" if current_position > 0.0 else "short",
                        entry_price=entry_price,
                        exit_price=exit_price,
                        size=entry_size,
                        return_pct=return_pct,
                        equity_after_trade=equity,
                        cost=abs(exit_price - close_price) + abs(entry_price - close_price),
                        reason=forced_exit_reason,
                    )
                )
                current_position = 0.0
                entry_price = None
                entry_timestamp = None
                bars_held = 0
            elif current_position == 0.0:
                risk_decision = self._evaluate_risk(
                    history=history,
                    equity=equity,
                    peak_equity=peak_equity,
                    current_position=current_position,
                    current_bar=current_bar,
                    bar_index=index,
                    signal=signal_value,
                    current_notional=max(0.0, current_position * close_price if current_position != 0.0 else 0.0),
                    open_positions=1 if current_position != 0.0 else 0,
                )
                if signal > 0 and risk_decision.allow_entry:
                    current_position = 1.0
                    entry_price = self._apply_cost(close_price, side="buy", cost_model=cost_model)
                    entry_timestamp = timestamp
                    entry_size = risk_decision.position_size
                elif signal < 0 and risk_decision.allow_entry:
                    current_position = -1.0
                    entry_price = self._apply_cost(close_price, side="sell", cost_model=cost_model)
                    entry_timestamp = timestamp
                    entry_size = risk_decision.position_size
                else:
                    entry_price = None
                    entry_timestamp = None
                    entry_size = 1.0
            elif current_position > 0.0 and signal <= 0:
                if entry_price is not None:
                    exit_price = self._apply_cost(close_price, side="sell", cost_model=cost_model)
                    return_pct = (exit_price - entry_price) / entry_price
                    trade_count += 1
                    if return_pct > 0:
                        wins += 1
                    equity *= 1 + entry_size * return_pct
                    trade_prices.append(exit_price)
                    trade_timestamps.append(timestamp)
                    trade_returns.append(return_pct)
                    trade_sizes.append(entry_size)
                    trade_costs.append(abs(exit_price - close_price) + abs(entry_price - close_price))
                    trade_records.append(
                        TradeRecord(
                            timestamp=timestamp,
                            side="long",
                            entry_price=entry_price,
                            exit_price=exit_price,
                            size=entry_size,
                            return_pct=return_pct,
                            equity_after_trade=equity,
                            cost=abs(exit_price - close_price) + abs(entry_price - close_price),
                        )
                    )
                current_position = 0.0
                entry_price = None
                entry_timestamp = None
            elif current_position < 0.0 and signal >= 0:
                if entry_price is not None:
                    exit_price = self._apply_cost(close_price, side="buy", cost_model=cost_model)
                    return_pct = (entry_price - exit_price) / entry_price
                    trade_count += 1
                    if return_pct > 0:
                        wins += 1
                    equity *= 1 + entry_size * return_pct
                    trade_prices.append(exit_price)
                    trade_timestamps.append(timestamp)
                    trade_returns.append(return_pct)
                    trade_sizes.append(entry_size)
                    trade_costs.append(abs(exit_price - close_price) + abs(entry_price - close_price))
                    trade_records.append(
                        TradeRecord(
                            timestamp=timestamp,
                            side="short",
                            entry_price=entry_price,
                            exit_price=exit_price,
                            size=entry_size,
                            return_pct=return_pct,
                            equity_after_trade=equity,
                            cost=abs(exit_price - close_price) + abs(entry_price - close_price),
                        )
                    )
                current_position = 0.0
                entry_price = None
                entry_timestamp = None
            elif current_position > 0.0 and signal <= 0:
                if entry_price is not None:
                    exit_price = self._apply_cost(close_price, side="sell", cost_model=cost_model)
                    return_pct = (exit_price - entry_price) / entry_price
                    trade_count += 1
                    if return_pct > 0:
                        wins += 1
                    equity *= 1 + entry_size * return_pct
                    trade_prices.append(exit_price)
                    trade_timestamps.append(timestamp)
                    trade_returns.append(return_pct)
                    trade_sizes.append(entry_size)
                    trade_costs.append(abs(exit_price - close_price) + abs(entry_price - close_price))
                    trade_records.append(
                        TradeRecord(
                            timestamp=timestamp,
                            side="long",
                            entry_price=entry_price,
                            exit_price=exit_price,
                            size=entry_size,
                            return_pct=return_pct,
                            equity_after_trade=equity,
                            cost=abs(exit_price - close_price) + abs(entry_price - close_price),
                        )
                    )
                current_position = 0.0
                entry_price = None
                entry_timestamp = None
            elif current_position < 0.0 and signal >= 0:
                if entry_price is not None:
                    exit_price = self._apply_cost(close_price, side="buy", cost_model=cost_model)
                    return_pct = (entry_price - exit_price) / entry_price
                    trade_count += 1
                    if return_pct > 0:
                        wins += 1
                    equity *= 1 + entry_size * return_pct
                    trade_prices.append(exit_price)
                    trade_timestamps.append(timestamp)
                    trade_returns.append(return_pct)
                    trade_sizes.append(entry_size)
                    trade_costs.append(abs(exit_price - close_price) + abs(entry_price - close_price))
                    trade_records.append(
                        TradeRecord(
                            timestamp=timestamp,
                            side="short",
                            entry_price=entry_price,
                            exit_price=exit_price,
                            size=entry_size,
                            return_pct=return_pct,
                            equity_after_trade=equity,
                            cost=abs(exit_price - close_price) + abs(entry_price - close_price),
                        )
                    )
                current_position = 0.0
                entry_price = None
                entry_timestamp = None

            peak_equity = max(peak_equity, equity)
            equity_series.append(equity)
            if current_position != 0.0 and entry_price is not None:
                price_move = (close_price - entry_price) / entry_price
                unrealized_pct = price_move if current_position > 0.0 else -price_move
                mtm_equity_series.append(equity * (1.0 + entry_size * unrealized_pct))
                position_series.append(current_position * entry_size)
            else:
                mtm_equity_series.append(equity)
                position_series.append(0.0)
            timestamps.append(timestamp)

        drawdown = 0.0
        if equity_series:
            drawdown = max((peak_equity - value) / peak_equity if peak_equity > 0 else 0.0 for value in equity_series)

        total_return = round(equity / self.initial_equity - 1.0, 10)
        win_rate = round(wins / trade_count, 10) if trade_count else 0.0
        metrics = build_performance_metrics(
            equity_series=equity_series,
            trade_returns=trade_returns,
            total_return=total_return,
            max_drawdown=drawdown,
            win_rate=win_rate,
        )

        return BacktestResult(
            total_return=total_return,
            win_rate=win_rate,
            max_drawdown=round(drawdown, 10),
            trades=trade_count,
            final_equity=round(equity, 10),
            equity_series=equity_series,
            timestamps=timestamps,
            trade_prices=trade_prices,
            trade_timestamps=trade_timestamps,
            trade_returns=trade_returns,
            trade_sizes=trade_sizes,
            trade_records=trade_records,
            trade_costs=trade_costs,
            mtm_equity_series=mtm_equity_series,
            position_series=position_series,
            metrics=metrics,
        )

    def _default_strategy(self, history: Sequence[Any], index: int, current_bar: Any) -> float | int | str | None:
        if len(history) < 2:
            return 0

        previous_bar = history[-2]
        current_close = _get_close(current_bar)
        previous_close = _get_close(previous_bar)
        if previous_close <= 0:
            return 0

        return_pct = (current_close - previous_close) / previous_close
        if return_pct > self.threshold:
            return 1
        if return_pct < -self.threshold:
            return -1
        return 0

    def _apply_cost(self, price: float, *, side: str, cost_model: CostModel | None) -> float:
        if cost_model is None:
            return price
        price_with_fees = cost_model.apply_trade_cost(price, side=side, role="taker", size=1.0)
        return cost_model.apply_fx_cost(price_with_fees, side=side, size=1.0)

    def _evaluate_risk(
        self,
        *,
        history: Sequence[Any],
        equity: float,
        peak_equity: float,
        current_position: float = 0.0,
        current_bar: Any | None = None,
        bar_index: int | None = None,
        signal: float | int | str | None = None,
        current_notional: float = 0.0,
        open_positions: int = 0,
    ) -> RiskDecision:
        if self.risk_manager is None:
            return RiskDecision(allow_entry=True, position_size=1.0)

        if signal is None:
            signal_side = None
        else:
            normalized_signal = _normalize_signal(signal)
            signal_side = "buy" if normalized_signal > 0 else "sell" if normalized_signal < 0 else None

        return self.risk_manager.evaluate(
            bars=history,
            equity=equity,
            peak_equity=peak_equity,
            current_position=current_position,
            current_bar=current_bar,
            bar_index=bar_index,
            signal_side=signal_side,
            current_notional=current_notional,
            open_positions=open_positions,
        )


def _normalize_signal(raw_signal: float | int | str | None) -> float:
    if raw_signal is None:
        return 0.0
    if isinstance(raw_signal, str):
        normalized = raw_signal.strip().lower()
        if normalized in {"buy", "long", "1", "true", "enter"}:
            return 1.0
        if normalized in {"sell", "short", "-1", "false", "exit"}:
            return -1.0
        return 0.0
    return float(raw_signal)


def _get_close(bar: Any) -> float:
    if hasattr(bar, "close"):
        return float(bar.close)
    if isinstance(bar, dict):
        return float(bar["close"])
    raise TypeError("bars must expose a close attribute or be dictionaries with a close key")


def _get_volume(bar: Any) -> float:
    if hasattr(bar, "volume"):
        return float(bar.volume)
    if isinstance(bar, dict):
        return float(bar.get("volume", 0.0))
    raise TypeError("bars must expose a volume attribute or be dictionaries with a volume key")


def _get_timestamp(bar: Any) -> datetime:
    if hasattr(bar, "timestamp"):
        return bar.timestamp
    if isinstance(bar, dict):
        return bar["timestamp"]
    raise TypeError("bars must expose a timestamp attribute or be dictionaries with a timestamp key")


def _get_exchange(bar: Any) -> str:
    if hasattr(bar, "exchange"):
        return str(bar.exchange)
    if isinstance(bar, dict):
        exchange = bar.get("exchange")
        if exchange is not None:
            return str(exchange)
    return "mock"
