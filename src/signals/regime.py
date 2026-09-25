"""Lightweight trending-vs-ranging regime classification.

Uses Kaufman's Efficiency Ratio (ER): the net price move over a lookback
window divided by the sum of bar-to-bar absolute moves over that same
window. ER close to 1 means price moved directly toward its endpoint
(trending); ER close to 0 means the same distance was covered with lots of
back-and-forth (ranging/choppy). This only needs close prices already in
`history`, so it's cheap to compute inline in a strategy and doesn't need a
new data pipeline.
"""

from __future__ import annotations

from typing import Any, Literal, Sequence

Regime = Literal["trending", "ranging"]


def compute_efficiency_ratio(closes: Sequence[float]) -> float:
    """Return Kaufman's Efficiency Ratio for a sequence of closes, in [0, 1]."""
    if len(closes) < 2:
        return 0.0

    net_move = abs(closes[-1] - closes[0])
    path_length = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    if path_length <= 0.0:
        return 0.0
    return net_move / path_length


def classify_regime(history: Sequence[Any], *, window: int = 20, trending_threshold: float = 0.3) -> Regime:
    """Classify the current regime from the last `window` bars of `history`.

    Args:
        trending_threshold: Efficiency Ratio at or above which the market is
            called "trending" rather than "ranging". 0.3 is a permissive
            default (Kaufman's own KAMA uses similar thresholds); raise it
            to demand a cleaner trend before gating momentum strategies on.
    """
    if len(history) < 2:
        return "ranging"

    recent_bars = history[-window:]
    closes = [_get_close(bar) for bar in recent_bars]
    efficiency_ratio = compute_efficiency_ratio(closes)
    return "trending" if efficiency_ratio >= trending_threshold else "ranging"


def _get_close(bar: Any) -> float:
    if hasattr(bar, "close"):
        return float(bar.close)
    if isinstance(bar, dict):
        return float(bar["close"])
    raise TypeError("bars must expose a close attribute or be dictionaries with a close key")
