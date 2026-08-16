"""Lightweight risk controls for the backtesting engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence


DEFAULT_EXCHANGE_RISK_LIMITS: dict[str, dict[str, Any]] = {
    "kraken": {
        "max_position_size": 0.5,
        "max_notional_per_trade": 500.0,
        "max_total_notional": 2500.0,
        "max_open_positions": 1,
        "max_open_orders": 2,
    },
    "firi": {
        "max_position_size": 0.35,
        "max_notional_per_trade": 400.0,
        "max_total_notional": 2000.0,
        "max_open_positions": 1,
        "max_open_orders": 2,
    },
    "sandbox": {
        "max_position_size": 1.0,
        "max_notional_per_trade": 1000.0,
        "max_total_notional": 5000.0,
        "max_open_positions": 3,
        "max_open_orders": 5,
    },
}

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
    enforce_quote_freshness: bool = False
    hard_stop_drawdown_pct: float = 0.02
    hard_stop_cooldown_bars: int = 5
    exchange_risk_limits: dict[str, dict[str, Any]] = field(default_factory=dict)
    # When True, skip execution-quality checks (spread/slippage/volatility) that
    # are only meaningful for live fills.  Paper trading does not have real
    # execution risk so these checks just prevent signals from filling.
    paper_mode: bool = False


@dataclass(slots=True)
class RiskDecision:
    """Result of evaluating whether a trade should be allowed."""

    allow_entry: bool
    position_size: float
    reason: str | None = None


@dataclass(slots=True)
class CircuitBreakerState:
    """Simple persisted state for an emergency hard stop."""

    active: bool = False
    reason: str | None = None
    triggered_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class CircuitBreaker:
    """A lightweight hard-stop controller that can pause trading immediately."""

    def __init__(self, *, state: CircuitBreakerState | None = None) -> None:
        """Initialize the object with its runtime state."""
        self.state = state or CircuitBreakerState()

    def activate(self, reason: str, **metadata: Any) -> None:
        """Activate the control and record the supplied reason."""
        self.state.active = True
        self.state.reason = reason
        self.state.triggered_at = datetime.now(timezone.utc)
        self.state.metadata = dict(metadata)

    def deactivate(self) -> None:
        """Deactivate the control and clear its state."""
        self.state.active = False
        self.state.reason = None
        self.state.triggered_at = None
        self.state.metadata = {}

    def is_active(self) -> bool:
        """Return whether the control is currently active."""
        return bool(self.state.active)

    def snapshot(self) -> dict[str, Any]:
        """Return a snapshot of the control state."""
        return {
            "active": self.state.active,
            "reason": self.state.reason,
            "triggered_at": self.state.triggered_at.isoformat() if self.state.triggered_at else None,
            "metadata": dict(self.state.metadata),
        }


class RiskManager:
    """Gate new entries using drawdown and volatility thresholds."""

    def __init__(self, config: RiskControlConfig | None = None) -> None:
        """Initialize the object with its runtime state."""
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
        circuit_breaker: CircuitBreaker | None = None,
        exchange_name: str | None = None,
        exchange_position_size: float = 0.0,
        current_exchange_notional: float = 0.0,
        exchange_open_positions: int = 0,
        open_orders_count: int = 0,
    ) -> RiskDecision:
        """Return whether a new position should be allowed and how large it should be."""
        if hard_stop_active:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="hard_stop")

        if circuit_breaker is not None and circuit_breaker.is_active():
            return RiskDecision(allow_entry=False, position_size=0.0, reason="circuit_breaker")

        if current_position != 0.0:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="position_open")

        if current_bar is not None and not self.config.paper_mode:
            slippage_pct = self._estimate_slippage_pct(current_bar)
            if slippage_pct > self.config.max_slippage_pct:
                return RiskDecision(allow_entry=False, position_size=0.0, reason="slippage_limit")

            spread_pct = self._estimate_spread_pct(current_bar)
            if spread_pct > self.config.max_spread_pct:
                return RiskDecision(allow_entry=False, position_size=0.0, reason="spread_limit")

            if self.config.enforce_quote_freshness and self._is_stale_quote(current_bar=current_bar, bars=bars, bar_index=bar_index):
                return RiskDecision(allow_entry=False, position_size=0.0, reason="stale_quote")

        drawdown = 0.0 if peak_equity <= 0.0 else max(0.0, (peak_equity - equity) / peak_equity)
        if drawdown > self.config.max_drawdown_pct:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="drawdown_limit")

        if drawdown > self.config.hard_stop_drawdown_pct:
            if circuit_breaker is not None:
                circuit_breaker.activate("hard_stop_drawdown", drawdown_pct=drawdown, threshold_pct=self.config.hard_stop_drawdown_pct)
            return RiskDecision(allow_entry=False, position_size=0.0, reason="hard_stop_drawdown")

        if open_positions >= self.config.max_open_positions:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="position_limit")

        exchange_caps = self._exchange_caps(exchange_name)
        exchange_max_open_positions = int(exchange_caps.get("max_open_positions", self.config.max_open_positions))
        exchange_max_open_orders = int(exchange_caps.get("max_open_orders", 5))
        max_total_notional = max(0.0, float(exchange_caps.get("max_total_notional", self.config.max_total_notional)))
        if exchange_open_positions >= exchange_max_open_positions:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="exchange_position_limit")
        if open_orders_count >= exchange_max_open_orders:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="open_order_limit")
        if max_total_notional > 0.0 and current_exchange_notional >= max_total_notional:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="exchange_notional_limit")

        volatility_pct = self._estimate_volatility(bars)
        if not self.config.paper_mode and volatility_pct > self.config.max_volatility_pct:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="volatility_limit")

        if volatility_pct <= 0.0:
            position_size = self._resolve_position_limit(exchange_name=exchange_name)
            position_size = self._apply_inventory_penalty(
                position_size=position_size,
                signal_side=signal_side,
                inventory_skew=inventory_skew,
            )
            position_size = self._apply_position_caps(
                position_size=position_size,
                current_notional=current_notional,
                equity=equity,
                exchange_name=exchange_name,
                current_exchange_notional=current_exchange_notional,
                exchange_position_size=exchange_position_size,
            )
            return RiskDecision(allow_entry=True, position_size=max(0.0, min(self._resolve_position_limit(exchange_name=exchange_name), position_size)))

        base_position_size = min(self._resolve_position_limit(exchange_name=exchange_name), self.config.risk_per_trade_pct / volatility_pct)
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
            exchange_name=exchange_name,
            current_exchange_notional=current_exchange_notional,
            exchange_position_size=exchange_position_size,
        )
        return RiskDecision(allow_entry=True, position_size=max(0.0, min(self._resolve_position_limit(exchange_name=exchange_name), position_size)))

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
            # No losses at all → no basis for Kelly to shrink sizing.
            # No wins at all → Kelly would say 0, but that blocks every entry.
            # Use a conservative fallback (kelly_fraction/2) so the caller
            # can still place a trade and gain real trade data.
            return self.config.kelly_fraction * 0.5 if positive_returns else self.config.kelly_fraction * 0.25

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

    def _apply_position_caps(
        self,
        *,
        position_size: float,
        current_notional: float,
        equity: float,
        exchange_name: str | None = None,
        current_exchange_notional: float = 0.0,
        exchange_position_size: float = 0.0,
    ) -> float:
        if position_size <= 0.0:
            return 0.0

        exchange_caps = self._exchange_caps(exchange_name)
        max_trade_notional = max(0.0, float(exchange_caps.get("max_notional_per_trade", self.config.max_notional_per_trade)))
        max_total_notional = max(0.0, float(exchange_caps.get("max_total_notional", self.config.max_total_notional)))
        if current_exchange_notional + max_trade_notional <= 0.0:
            return position_size

        if max_trade_notional > 0.0 and current_exchange_notional > max_trade_notional:
            return 0.0

        available_capacity = max(0.0, max_total_notional - current_exchange_notional)
        if max_total_notional > 0.0 and available_capacity <= 0.0:
            return 0.0

        if max_total_notional > 0.0:
            max_allowed = min(position_size, available_capacity / max(1.0, equity))
            return max(0.0, max_allowed)

        if exchange_position_size >= self._resolve_position_limit(exchange_name=exchange_name):
            return 0.0

        return position_size

    def _resolve_position_limit(self, *, exchange_name: str | None) -> float:
        exchange_caps = self._exchange_caps(exchange_name)
        return max(0.0, float(exchange_caps.get("max_position_size", self.config.max_position_size)))

    def _exchange_caps(self, exchange_name: str | None) -> dict[str, Any]:
        normalized_name = (exchange_name or "").strip().lower()
        if not normalized_name:
            return {}
        configured = self.config.exchange_risk_limits.get(normalized_name, {})
        if configured:
            return configured
        defaults = DEFAULT_EXCHANGE_RISK_LIMITS.get(normalized_name, {})
        return dict(defaults)

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
