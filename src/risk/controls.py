"""Lightweight risk controls for the backtesting engine."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(slots=True)
class RiskControlConfig:
    """Simple risk constraints for position entry and sizing."""

    max_drawdown_pct: float = 0.25
    max_volatility_pct: float = 0.10
    risk_per_trade_pct: float = 0.02
    max_position_size: float = 1.0
    volatility_window: int = 10


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

    def evaluate(self, *, bars: Sequence[Any], equity: float, peak_equity: float, current_position: float = 0.0) -> RiskDecision:
        """Return whether a new position should be allowed and how large it should be."""
        if current_position != 0.0:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="position_open")

        drawdown = 0.0 if peak_equity <= 0.0 else max(0.0, (peak_equity - equity) / peak_equity)
        if drawdown > self.config.max_drawdown_pct:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="drawdown_limit")

        volatility_pct = self._estimate_volatility(bars)
        if volatility_pct > self.config.max_volatility_pct:
            return RiskDecision(allow_entry=False, position_size=0.0, reason="volatility_limit")

        if volatility_pct <= 0.0:
            return RiskDecision(allow_entry=True, position_size=self.config.max_position_size)

        position_size = min(self.config.max_position_size, self.config.risk_per_trade_pct / volatility_pct)
        return RiskDecision(allow_entry=True, position_size=max(0.0, position_size))

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

        return float(statistics.pstdev(returns))


def _get_close(bar: Any) -> float:
    if hasattr(bar, "close"):
        return float(bar.close)
    if isinstance(bar, dict):
        return float(bar["close"])
    raise TypeError("bars must expose a close attribute or be dictionaries with a close key")
