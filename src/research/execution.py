"""Order-fill simulation for research backtests: taker fills at the close, or realistic resting limit orders.

`SimpleBacktester` fills every change of position at the signal bar's close.
That is what a taker (market) order roughly gets, but it flatters a maker
(limit) strategy twice: every limit order fills, and it fills at the price
you wanted. Real post-only orders only fill when the market comes to them,
which means

- **missed trades**: when price runs away in your direction without coming
  back, the order never fills, and those are usually the best trades;
- **adverse selection**: a buy limit fills when price is falling, so a filled
  trade starts out losing more often than a market order would.

`simulate_fills` turns a series of target positions into fills under a
`FillModel`, with a cash-and-units account marked to market every bar and
funding charged on the held notional, and returns a `BacktestResult` so the
research metrics apply unchanged. Assumptions, deliberately conservative:

- A buy limit at price L fills during a later bar only if that bar's low is
  at or below L minus `through_bps` (the market must trade through L, not
  just touch it, since orders ahead in the queue fill first). Sells mirror it.
- Fills are all-or-nothing at L (no partial fills, no price improvement).
- The limit sits at the signal bar's close shifted by `limit_offset_bps` in
  your favour (0 = at the close, i.e. about the far touch of a tight book).
- Positions are full size (target 1 = 100% of equity, -1 = short 100%),
  rebalanced to the target notional at each fill.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from src.backtest.analytics import build_performance_metrics
from src.backtest.indicators import series
from src.backtest.simple_backtest import BacktestResult, TradeRecord

FILL_STYLES = ("close", "maker")
TIMEOUT_ACTIONS = ("cancel", "requote", "taker")


@dataclass(frozen=True, slots=True)
class FillModel:
    """How a change of target position becomes a fill.

    Attributes:
        style: "close" fills at the signal bar's close as a taker order (fee +
            slippage), which matches `SimpleBacktester`. "maker" rests a
            post-only limit order and fills it only when the market trades
            through it.
        max_wait_bars: Bars a limit order rests before `on_timeout` applies.
        through_bps: How far past the limit a bar must trade before the order
            counts as filled (queue position).
        limit_offset_bps: Place the limit this much better than the signal
            close (buy lower, sell higher). Higher = cheaper fills, fewer of them.
        on_timeout: "cancel" gives up until the target changes, "requote"
            moves the limit to the latest close and keeps waiting, "taker"
            crosses the spread at that bar's close (fee + slippage).
    """

    style: str = "close"
    max_wait_bars: int = 1
    through_bps: float = 1.0
    limit_offset_bps: float = 0.0
    on_timeout: str = "cancel"

    def __post_init__(self) -> None:
        """Reject settings that don't describe an order type."""
        if self.style not in FILL_STYLES:
            raise ValueError(f"style must be one of {FILL_STYLES}")
        if self.on_timeout not in TIMEOUT_ACTIONS:
            raise ValueError(f"on_timeout must be one of {TIMEOUT_ACTIONS}")
        if self.max_wait_bars < 1:
            raise ValueError("max_wait_bars must be at least 1")

    @classmethod
    def maker(cls, *, max_wait_bars: int = 1, on_timeout: str = "cancel", through_bps: float = 1.0, limit_offset_bps: float = 0.0) -> "FillModel":
        """A resting post-only limit order at the signal close."""
        return cls(style="maker", max_wait_bars=max_wait_bars, through_bps=through_bps, limit_offset_bps=limit_offset_bps, on_timeout=on_timeout)


@dataclass(slots=True)
class FillStats:
    """What happened to the orders: how many filled as maker or taker, how many were given up, how long they waited."""

    orders: int = 0
    maker_fills: int = 0
    taker_fills: int = 0
    cancelled: int = 0
    bars_waited: list[int] = field(default_factory=list)

    @property
    def fill_rate(self) -> float:
        """Share of orders that ended in a fill."""
        return (self.maker_fills + self.taker_fills) / self.orders if self.orders else float("nan")

    def as_dict(self) -> dict[str, float]:
        """Summary numbers for tables."""
        return {
            "orders": float(self.orders),
            "maker_fills": float(self.maker_fills),
            "taker_fills": float(self.taker_fills),
            "cancelled": float(self.cancelled),
            "fill_rate": self.fill_rate,
            "avg_bars_to_fill": float(np.mean(self.bars_waited)) if self.bars_waited else float("nan"),
        }


@dataclass(slots=True)
class _WorkingOrder:
    target: float
    limit: float
    placed_at: int
    waited: int = 0


def simulate_fills(
    bars: Sequence[Any],
    targets: Sequence[float],
    *,
    taker_fee_pct: float,
    maker_fee_pct: float,
    slippage_bps: float = 0.0,
    funding_pct_per_day: float = 0.0,
    fills: FillModel | None = None,
    initial_equity: float = 1000.0,
) -> tuple[BacktestResult, FillStats]:
    """Trade `targets` (position per bar, decided at that bar's close) under `fills`; return the result and fill stats.

    The position is closed at the last bar's close as a taker order so every
    trade is realised, like `SimpleBacktester`.
    """
    model = fills or FillModel()
    close, high, low = series(bars, "close"), series(bars, "high"), series(bars, "low")
    timestamps: list[datetime] = [bar.timestamp for bar in bars]
    interval_seconds = float(getattr(bars[0], "interval_seconds", 86400) or 86400) if bars else 86400.0
    funding_per_bar = funding_pct_per_day / 100.0 * interval_seconds / 86400.0
    taker_fee, maker_fee, slippage = taker_fee_pct / 100.0, maker_fee_pct / 100.0, slippage_bps / 10_000.0
    through, offset = model.through_bps / 10_000.0, model.limit_offset_bps / 10_000.0

    count = len(bars)
    cash, units, position = initial_equity, 0.0, 0.0
    equity_series = np.empty(count)
    position_series = np.zeros(count)
    trades: list[TradeRecord] = []
    stats = FillStats()
    working: _WorkingOrder | None = None
    abandoned_target: float | None = None  # after a cancel timeout, don't chase this target until it changes
    entry_price: float | None = None
    entry_fees = 0.0
    entry_equity = initial_equity
    entry_kinds: list[str] = []

    def execute(index: int, target: float, price: float, kind: str) -> None:
        nonlocal cash, units, position, entry_price, entry_fees, entry_equity, entry_kinds
        equity_now = cash + units * price
        new_units = target * equity_now / price
        fee = abs(new_units - units) * price * (maker_fee if kind == "maker" else taker_fee)
        if position != 0.0 and (target == 0.0 or np.sign(target) != np.sign(position)):
            side = "long" if position > 0 else "short"
            exit_fee = abs(units) * price * (maker_fee if kind == "maker" else taker_fee)
            gross = (price / entry_price - 1.0) * np.sign(position)
            net = gross - (entry_fees + exit_fee) / max(entry_equity, 1e-12)
            trades.append(
                TradeRecord(
                    timestamp=timestamps[index],
                    side=side,
                    entry_price=float(entry_price),
                    exit_price=float(price),
                    size=abs(position),
                    return_pct=float(net),
                    equity_after_trade=float(equity_now - fee),
                    cost=float(entry_fees + exit_fee),
                    reason="+".join(entry_kinds + [kind]),
                )
            )
            entry_price = None
        cash -= (new_units - units) * price + fee
        if target != 0.0 and (position == 0.0 or np.sign(target) != np.sign(position)):
            entry_price, entry_equity, entry_kinds = price, equity_now, [kind]
            entry_fees = abs(new_units) * price * (maker_fee if kind == "maker" else taker_fee)
        units, position = new_units, target

    for index in range(count):
        # 1. A resting limit order may fill inside this bar.
        if working is not None and index > working.placed_at:
            buying = working.target > position
            filled = low[index] <= working.limit * (1.0 - through) if buying else high[index] >= working.limit * (1.0 + through)
            if filled:
                execute(index, working.target, working.limit, "maker")
                stats.maker_fills += 1
                stats.bars_waited.append(working.waited + 1)
                working = None
            else:
                working.waited += 1
                if working.waited >= model.max_wait_bars:
                    if model.on_timeout == "taker":
                        execute(index, working.target, close[index] * (1.0 + slippage if buying else 1.0 - slippage), "taker")
                        stats.taker_fills += 1
                        stats.bars_waited.append(working.waited)
                        working = None
                    elif model.on_timeout == "requote":
                        working.limit = close[index] * (1.0 - offset if buying else 1.0 + offset)
                        working.waited = 0
                        working.placed_at = index
                    else:
                        stats.cancelled += 1
                        abandoned_target = working.target
                        working = None

        # 2. At the close, act on the strategy's target for this bar.
        target = float(targets[index]) if index < count - 1 else 0.0
        if abandoned_target is not None and target != abandoned_target:
            abandoned_target = None
        if index == count - 1:
            working = None
            if position != 0.0:
                execute(index, 0.0, close[index] * (1.0 - slippage if position > 0 else 1.0 + slippage), "taker")
        elif model.style == "close":
            if target != position:
                stats.orders += 1
                buying = target > position
                execute(index, target, close[index] * (1.0 + slippage if buying else 1.0 - slippage), "taker")
                stats.taker_fills += 1
                stats.bars_waited.append(0)
        elif target != position and target != abandoned_target and (working is None or working.target != target):
            if working is not None:
                stats.cancelled += 1
            stats.orders += 1
            buying = target > position
            working = _WorkingOrder(target=target, limit=close[index] * (1.0 - offset if buying else 1.0 + offset), placed_at=index)
        elif target == position and working is not None:
            stats.cancelled += 1
            working = None

        # 3. Mark to market and charge funding on the notional held into the next bar.
        if units != 0.0:
            cash -= units * close[index] * funding_per_bar
        equity_series[index] = cash + units * close[index]
        position_series[index] = position

    equity = equity_series.tolist()
    peak = np.maximum.accumulate(equity_series)
    drawdown = float(np.max(1.0 - equity_series / peak)) if count else 0.0
    trade_returns = [trade.return_pct for trade in trades]
    wins = sum(1 for value in trade_returns if value > 0.0)
    total_return = equity[-1] / initial_equity - 1.0 if count else 0.0
    win_rate = wins / len(trades) if trades else 0.0
    result = BacktestResult(
        total_return=round(total_return, 10),
        win_rate=round(win_rate, 10),
        max_drawdown=round(drawdown, 10),
        trades=len(trades),
        final_equity=round(equity[-1], 10) if count else initial_equity,
        equity_series=equity,
        timestamps=timestamps,
        trade_returns=trade_returns,
        trade_records=trades,
        metrics=build_performance_metrics(equity_series=equity, trade_returns=trade_returns, total_return=total_return, max_drawdown=drawdown, win_rate=win_rate),
        mtm_equity_series=equity,
        position_series=position_series.tolist(),
    )
    return result, stats
