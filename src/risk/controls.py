"""Lightweight risk controls for the backtesting engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np


@dataclass(slots=True)
class RiskControlConfig:
    """Simple risk constraints for position entry and sizing."""

    max_drawdown_pct: float = 0.25
    max_volatility_pct: float = 0.10
    risk_per_trade_pct: float = 0.02
    max_position_size: float = 1.0
    volatility_window: int = 10
    kelly_fraction: float = 0.5
    kelly_window: int = 20
    max_slippage_pct: float = 0.03
    max_spread_pct: float = 0.03
    max_quote_age_seconds: int = 900
    inventory_skew_threshold: float = 0.5
    inventory_penalty_factor: float = 0.5
    max_notional_per_trade: float = 1000.0
    max_total_notional: float = 5000.0
    max_open_positions: int = 3
    hard_stop_drawdown_pct: float = 0.02
    hard_stop_cooldown_bars: int = 5


@dataclass(slots=True)
class RiskDecision:
    """Result of evaluating whether a trade should be allowed."""

    allow_entry: bool
    position_size: float
    reason: str | None = None


class RiskManager:
    """Gate new entries using drawdown and volatility thresholds."""

    def __init__(self, config: RiskControlConfig | None = None) -> None:
        self.config = config or RiskControlConfig()

    def evaluate(
        self,
        *,
        bars: Sequence[Any],
        equity: float,
        peak_equity: float,
        current_position: float = 0.0,
        current_bar: Any | None = None,
        bar_index: int | None = None,
        signal_side: str | None = None,
        inventory_skew: float = 0.0,
        current_notional: float = 0.0,
        open_positions: int = 0,
        hard_stop_active: bool = False,
        cooldown_bars_remaining: int = 0,
    ) -> RiskDecision:
        """Return whether a new position should be allowed and how large it should be."""
        if hard_stop_active:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="hard_stop")

        if current_position != 0.0:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="position_open")

        if current_bar is not None:
            slippage_pct = self._estimate_slippage_pct(current_bar)
            if slippage_pct > self.config.max_slippage_pct:
                return RiskDecision(allow_entry=False, position_size=0.0, reason="slippage_limit")

            spread_pct = self._estimate_spread_pct(current_bar)
            if spread_pct > self.config.max_spread_pct:
                return RiskDecision(allow_entry=False, position_size=0.0, reason="spread_limit")

            if self._is_stale_quote(current_bar=current_bar, bars=bars, bar_index=bar_index):
                return RiskDecision(allow_entry=False, position_size=0.0, reason="stale_quote")

        drawdown = 0.0 if peak_equity <= 0.0 else max(0.0, (peak_equity - equity) / peak_equity)
        if drawdown > self.config.max_drawdown_pct:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="drawdown_limit")

        if drawdown > self.config.hard_stop_drawdown_pct:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="hard_stop_drawdown")

        if open_positions >= self.config.max_open_positions:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="position_limit")

        volatility_pct = self._estimate_volatility(bars)
        if volatility_pct > self.config.max_volatility_pct:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="volatility_limit")

        if volatility_pct <= 0.0:
            position_size = self.config.max_position_size
            position_size = self._apply_inventory_penalty(
                position_size=position_size,
                signal_side=signal_side,
                inventory_skew=inventory_skew,
            )
            position_size = self._apply_position_caps(
                position_size=position_size,
                current_notional=current_notional,
                equity=equity,
            )
            return RiskDecision(allow_entry=True, position_size=max(0.0, min(self.config.max_position_size, position_size)))

        base_position_size = min(self.config.max_position_size, self.config.risk_per_trade_pct / volatility_pct)
        kelly_multiplier = self._estimate_kelly_multiplier(bars)
        position_size = base_position_size * kelly_multiplier
        position_size = self._apply_inventory_penalty(
            position_size=position_size,
            signal_side=signal_side,
            inventory_skew=inventory_skew,
        )
        position_size = self._apply_position_caps(
            position_size=position_size,
            current_notional=current_notional,
            equity=equity,
        )
        return RiskDecision(allow_entry=True, position_size=max(0.0, min(self.config.max_position_size, position_size)))

    def _estimate_volatility(self, bars: Sequence[Any]) -> float:
        if len(bars) < 2:
            return 0.0

        closes = [_get_close(bar) for bar in bars[-self.config.volatility_window :]]
        if len(closes) < 2:
            return 0.0

        returns = []
        for index in range(1, len(closes)):
            previous_close = closes[index - 1]
            current_close = closes[index]
            if previous_close <= 0.0:
                continue
            returns.append((current_close - previous_close) / previous_close)

        if not returns:
            return 0.0

        return float(np.std(returns, ddof=0))

    def _estimate_kelly_multiplier(self, bars: Sequence[Any]) -> float:
        if len(bars) < 2:
            return 1.0

        closes = [_get_close(bar) for bar in bars[-self.config.kelly_window :]]
        returns = []
        for index in range(1, len(closes)):
            previous_close = closes[index - 1]
            current_close = closes[index]
            if previous_close <= 0.0:
                continue
            returns.append((current_close - previous_close) / previous_close)

        if len(returns) < 2:
            return 1.0 if any(value > 0.0 for value in returns) else 0.0

        positive_returns = [value for value in returns if value > 0.0]
        negative_returns = [abs(value) for value in returns if value < 0.0]
        if not positive_returns or not negative_returns:
            return 1.0 if positive_returns else 0.0

        win_rate = len(positive_returns) / len(returns)
        avg_win = float(np.mean(positive_returns))
        avg_loss = float(np.mean(negative_returns))
        if avg_loss <= 0.0:
            return 1.0 if avg_win > 0.0 else 0.0

        win_loss_ratio = avg_win / avg_loss
        kelly_fraction = (win_rate * win_loss_ratio - (1.0 - win_rate)) / win_loss_ratio
        kelly_fraction = max(0.0, min(1.0, kelly_fraction))

        return max(0.0, min(1.0, kelly_fraction * self.config.kelly_fraction))

    def _apply_inventory_penalty(self, *, position_size: float, signal_side: str | None, inventory_skew: float) -> float:
        if position_size <= 0.0:
            return 0.0

        normalized_side = (signal_side or "").strip().lower()
        if normalized_side not in {"buy", "long", "1", "true", "enter"}:
            return position_size

        normalized_skew = max(0.0, inventory_skew)
        if normalized_skew <= self.config.inventory_skew_threshold:
            return position_size

        skew_ratio = (normalized_skew - self.config.inventory_skew_threshold) / max(1e-9, 1.0 - self.config.inventory_skew_threshold)
        penalty = min(1.0, skew_ratio * self.config.inventory_penalty_factor)
        return max(0.0, position_size * (1.0 - penalty))

    def _apply_position_caps(self, *, position_size: float, current_notional: float, equity: float) -> float:
        if position_size <= 0.0:
            return 0.0

        max_trade_notional = max(0.0, self.config.max_notional_per_trade)
        if current_notional + max_trade_notional <= 0.0:
            return position_size

        if max_trade_notional > 0.0 and current_notional > max_trade_notional:
            return 0.0

        available_capacity = max(0.0, self.config.max_total_notional - current_notional)
        if self.config.max_total_notional > 0.0 and available_capacity <= 0.0:
            return 0.0

        if self.config.max_total_notional > 0.0:
            max_allowed = min(position_size, available_capacity / max(1.0, equity))
            return max(0.0, max_allowed)

        return position_size

    def _estimate_slippage_pct(self, bar: Any) -> float:
        close = _get_close(bar)
        if close <= 0.0:
            return 0.0
        return abs(close - _get_open(bar)) / close

    def _estimate_spread_pct(self, bar: Any) -> float:
        close = _get_close(bar)
        if close <= 0.0:
            return 0.0
        return abs(_get_high(bar) - _get_low(bar)) / close

    def _is_stale_quote(self, *, current_bar: Any, bars: Sequence[Any], bar_index: int | None) -> bool:
        if self.config.max_quote_age_seconds <= 0:
            return False
        if not bars:
            return False

        resolved_index = len(bars) - 1 if bar_index is None else bar_index
        if resolved_index <= 0 or resolved_index >= len(bars):
            return False

        current_timestamp = _get_timestamp(current_bar)
        previous_timestamp = _get_timestamp(bars[resolved_index - 1])
        if current_timestamp is None or previous_timestamp is None:
            return False

        return (current_timestamp - previous_timestamp).total_seconds() > self.config.max_quote_age_seconds


def _get_close(bar: Any) -> float:
    if hasattr(bar, "close"):
        return float(bar.close)
    if isinstance(bar, dict):
        return float(bar["close"])
    raise TypeError("bars must expose a close attribute or be dictionaries with a close key")


def _get_open(bar: Any) -> float:
    if hasattr(bar, "open"):
        return float(bar.open)
    if isinstance(bar, dict):
        return float(bar["open"])
    raise TypeError("bars must expose an open attribute or be dictionaries with an open key")


def _get_high(bar: Any) -> float:
    if hasattr(bar, "high"):
        return float(bar.high)
    if isinstance(bar, dict):
        return float(bar["high"])
    raise TypeError("bars must expose a high attribute or be dictionaries with a high key")


def _get_low(bar: Any) -> float:
    if hasattr(bar, "low"):
        return float(bar.low)
    if isinstance(bar, dict):
        return float(bar["low"])
    raise TypeError("bars must expose a low attribute or be dictionaries with a low key")


def _get_timestamp(bar: Any) -> Any | None:
    if hasattr(bar, "timestamp"):
        return getattr(bar, "timestamp")
    if isinstance(bar, dict):
        return bar.get("timestamp")
    return None
