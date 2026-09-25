"""Strategy library: every signal factory the backtester and runtime can run.

Each factory takes its parameters and returns a `StrategyFn` with the
signature ``(history, index, current_bar) -> -1 | 0 | 1``. The return value
is the *target position*, not a one-off order: 1 = be long, -1 = be short,
0 = be flat. The engine opens a position when the signal turns non-zero and
closes it as soon as the signal is 0 or flips, so a strategy that wants to
hold must keep returning 1 on every bar it wants to stay in.

Register new strategies in `StrategyRegistry` (src/backtest/runner.py) so the
runtime can run them by name, and in `src/research/catalog.py` so the
research tools can sweep them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from src.backtest.indicators import atr, linreg_tstat, rolling_max, rolling_mean, rolling_min, rolling_quantile, rolling_std, rsi, series, shift
from src.backtest.simple_backtest import StrategyFn, _get_close, _get_volume, _normalize_signal


def moving_average_crossover_strategy(short_window: int = 3, long_window: int = 6) -> StrategyFn:
    """Create a simple moving-average crossover strategy for any OHLC-like series."""

    def strategy(history: Sequence[Any], index: int, current_bar: Any) -> float | int | str | None:
        """Generate the signal strategy output for the current market context."""
        if len(history) < max(short_window, long_window):
            return 0
        closes = [_get_close(bar) for bar in history[-long_window:]]
        short_ma = sum(closes[-short_window:]) / short_window
        long_ma = sum(closes) / len(closes)
        if short_ma > long_ma:
            return 1
        if short_ma < long_ma:
            return -1
        return 0

    return strategy


def momentum_breakout_strategy(lookback: int = 5, threshold: float = 0.01) -> StrategyFn:
    """Create a simple momentum breakout strategy for any OHLC-like series."""

    def strategy(history: Sequence[Any], index: int, current_bar: Any) -> float | int | str | None:
        """Generate a long/short signal based on recent momentum."""
        if len(history) < lookback + 1:
            return 0

        baseline_bar = history[-lookback - 1]
        current_close = _get_close(current_bar)
        baseline_close = _get_close(baseline_bar)
        if baseline_close <= 0:
            return 0

        return_pct = (current_close - baseline_close) / baseline_close
        if return_pct > threshold:
            return 1
        if return_pct < -threshold:
            return -1
        return 0

    return strategy


def signal_trend_strategy(lookback: int = 1, threshold: float = 0.001) -> StrategyFn:
    """Create a simple signal-following strategy that reacts quickly to recent price moves."""

    def strategy(history: Sequence[Any], index: int, current_bar: Any) -> float | int | str | None:
        """Generate a long/short signal based on the recent price change."""
        if len(history) < lookback + 1:
            return 0

        baseline_bar = history[-lookback - 1]
        current_close = _get_close(current_bar)
        baseline_close = _get_close(baseline_bar)
        if baseline_close <= 0:
            return 0

        return_pct = (current_close - baseline_close) / baseline_close
        if return_pct > threshold:
            return 1
        if return_pct < -threshold:
            return -1
        return 0

    return strategy


def volume_confirmed_momentum_strategy(
    lookback: int = 5,
    threshold: float = 0.01,
    volume_window: int = 10,
    volume_multiplier: float = 1.5,
    short_threshold: float | None = None,
    short_volume_multiplier: float | None = None,
    allow_short: bool = True,
) -> StrategyFn:
    """Create a momentum strategy that only signals when the move is confirmed by above-average volume.

    Args:
        short_threshold: Price-move threshold required for a short signal. Defaults
            to ``threshold`` (symmetric). Pass a larger value to require a bigger
            confirmed drop before shorting than before going long.
        short_volume_multiplier: Volume-spike multiplier required for a short
            signal. Defaults to ``volume_multiplier`` (symmetric). Pass a larger
            value to require heavier volume confirmation before shorting.
        allow_short: When False, never emit a short signal at all (flat instead),
            regardless of how the price/volume conditions resolve.
    """
    resolved_short_threshold = threshold if short_threshold is None else short_threshold
    resolved_short_volume_multiplier = volume_multiplier if short_volume_multiplier is None else short_volume_multiplier

    def strategy(history: Sequence[Any], index: int, current_bar: Any) -> float | int | str | None:
        """Generate a long/short signal only when price momentum is confirmed by a volume spike."""
        if len(history) < lookback + 1:
            return 0

        baseline_bar = history[-lookback - 1]
        current_close = _get_close(current_bar)
        baseline_close = _get_close(baseline_bar)
        if baseline_close <= 0:
            return 0

        return_pct = (current_close - baseline_close) / baseline_close
        is_bullish = return_pct > 0
        active_threshold = threshold if is_bullish else resolved_short_threshold
        if abs(return_pct) <= active_threshold:
            return 0
        if not is_bullish and not allow_short:
            return 0

        # Compare this bar's volume to the average of the *preceding* bars,
        # not including this bar itself - including it would dilute the
        # baseline with the very spike this signal is trying to detect.
        prior_bars = history[-volume_window - 1 : -1]
        if len(prior_bars) < volume_window:
            return 0
        avg_volume = sum(_get_volume(bar) for bar in prior_bars) / len(prior_bars)
        current_volume = _get_volume(current_bar)
        active_multiplier = volume_multiplier if is_bullish else resolved_short_volume_multiplier
        if avg_volume <= 0.0 or current_volume < avg_volume * active_multiplier:
            return 0

        return 1 if is_bullish else -1

    return strategy


def volume_confirmed_momentum_biased_strategy(
    lookback: int = 5,
    threshold: float = 0.01,
    volume_window: int = 10,
    volume_multiplier: float = 1.5,
    short_threshold_multiplier: float = 2.0,
    short_volume_multiplier_factor: float = 1.5,
) -> StrategyFn:
    """Long-biased variant of volume_confirmed_momentum_strategy.

    Longs use the plain thresholds; shorts require a bigger confirmed move
    (``threshold * short_threshold_multiplier``) and heavier volume
    confirmation (``volume_multiplier * short_volume_multiplier_factor``),
    so the strategy leans long without being long-only.
    """
    return volume_confirmed_momentum_strategy(
        lookback=lookback,
        threshold=threshold,
        volume_window=volume_window,
        volume_multiplier=volume_multiplier,
        short_threshold=threshold * short_threshold_multiplier,
        short_volume_multiplier=volume_multiplier * short_volume_multiplier_factor,
    )


def band_reversion_strategy(window: int = 20, num_std: float = 2.0, allow_short: bool = True) -> StrategyFn:
    """Create a mean-reversion strategy that trades against moves outside a rolling price band.

    Computes a rolling mean and standard deviation over `window` bars
    (a Bollinger-Band-style channel) and bets on reversion back toward the
    mean: buy when price closes below the lower band, sell/short when it
    closes above the upper band. This is deliberately the opposite bet to
    the momentum-family strategies above - useful when a market is ranging
    rather than trending.
    """

    def strategy(history: Sequence[Any], index: int, current_bar: Any) -> float | int | str | None:
        """Generate a reversion signal when price closes outside the rolling band."""
        if len(history) < window + 1:
            return 0

        prior_closes = [_get_close(bar) for bar in history[-window - 1 : -1]]
        band_mean = sum(prior_closes) / len(prior_closes)
        variance = sum((close - band_mean) ** 2 for close in prior_closes) / len(prior_closes)
        band_std = variance**0.5
        if band_std <= 0.0:
            return 0

        current_close = _get_close(current_bar)
        lower_band = band_mean - num_std * band_std
        upper_band = band_mean + num_std * band_std
        if current_close < lower_band:
            return 1
        if current_close > upper_band and allow_short:
            return -1
        return 0

    return strategy


def donchian_breakout_strategy(entry_window: int = 20, exit_window: int = 10, allow_short: bool = True) -> StrategyFn:
    """Turtle-style channel breakout: enter on a new N-bar extreme, hold until an opposite M-bar extreme.

    Trend-following on price extremes rather than on averages or a fixed %
    move: go long when the close clears the highest high of the prior
    `entry_window` bars and stay long until the close breaks the lowest low
    of the prior `exit_window` bars (mirror image for shorts). A shorter
    exit channel than entry channel lets winners run while giving back less
    on a reversal than waiting for the full entry channel to break.
    """
    warmup = max(entry_window, exit_window) + 1

    def strategy(history: Sequence[Any], index: int, current_bar: Any) -> float | int | str | None:
        """Hold the position implied by the most recent channel breakout that hasn't been exited."""
        if len(history) < warmup:
            return 0
        close, high, low = series(history, "close"), series(history, "high"), series(history, "low")
        prior_entry_high = shift(rolling_max(high, entry_window))
        prior_entry_low = shift(rolling_min(low, entry_window))
        prior_exit_high = shift(rolling_max(high, exit_window))
        prior_exit_low = shift(rolling_min(low, exit_window))
        return latch_position(
            long_entry=close > prior_entry_high,
            long_exit=close < prior_exit_low,
            short_entry=close < prior_entry_low if allow_short else None,
            short_exit=close > prior_exit_high if allow_short else None,
        )

    return strategy


def keltner_breakout_strategy(window: int = 20, atr_multiplier: float = 2.0, allow_short: bool = True) -> StrategyFn:
    """Volatility-scaled breakout: enter when price moves `atr_multiplier` ATRs beyond its average, exit back at the average.

    Unlike `momentum_breakout` (a fixed % threshold, which means very
    different things for BTC vs SOL or 4h vs daily bars), the threshold here
    scales with each market's own recent true range. Holds until the close
    crosses back through the moving average. The channel is built from the
    prior bars only, so a breakout bar can't widen its own threshold.
    """
    warmup = window + 2

    def strategy(history: Sequence[Any], index: int, current_bar: Any) -> float | int | str | None:
        """Hold after a close outside the ATR channel until the close returns through the middle line."""
        if len(history) < warmup:
            return 0
        close, high, low = series(history, "close"), series(history, "high"), series(history, "low")
        middle = shift(rolling_mean(close, window))
        band = atr_multiplier * shift(atr(high, low, close, window))
        return latch_position(
            long_entry=close > middle + band,
            long_exit=close < middle,
            short_entry=close < middle - band if allow_short else None,
            short_exit=close > middle if allow_short else None,
        )

    return strategy


def trend_tstat_strategy(window: int = 24, strength_threshold: float = 1.5, allow_short: bool = True) -> StrategyFn:
    """Statistical trend filter: enter when the log-price slope is strong relative to the noise around it.

    Uses the regression-slope t-statistic divided by sqrt(window) as a
    scale-free trend score. Raw slope t-stats on prices grow with the window
    length (spurious-regression effect), so the division makes one
    threshold mean the same thing at any window: on BTC/SOL 4h bars the
    score's median is roughly 0.7-0.9 and its 90th percentile roughly
    1.6-2.3. The t-stat rewards trends that are steep *and* clean; a choppy
    path to the same endpoint scores low, which a plain N-bar return
    (momentum_breakout) or MA crossover can't tell apart. Enters above
    `strength_threshold` and holds until the slope's sign flips
    (hysteresis, so it doesn't churn around the threshold).
    """
    warmup = window
    scale = float(np.sqrt(window))

    def strategy(history: Sequence[Any], index: int, current_bar: Any) -> float | int | str | None:
        """Hold in the direction of a strong, clean trend until the trend's slope changes sign."""
        if len(history) < warmup:
            return 0
        score = linreg_tstat(np.log(series(history, "close")), window) / scale
        return latch_position(
            long_entry=score > strength_threshold,
            long_exit=score < 0.0,
            short_entry=score < -strength_threshold if allow_short else None,
            short_exit=score > 0.0 if allow_short else None,
        )

    return strategy


def volatility_squeeze_strategy(
    window: int = 20,
    num_std: float = 2.0,
    squeeze_lookback: int = 60,
    squeeze_quantile: float = 0.2,
    squeeze_memory: int = 5,
    allow_short: bool = True,
) -> StrategyFn:
    """Trade the breakout out of a low-volatility squeeze.

    Volatility clusters: unusually quiet stretches tend to end in a sharp
    move. A "squeeze" is when the Bollinger bandwidth sits in the lowest
    `squeeze_quantile` of its last `squeeze_lookback` bars. If one of the
    last `squeeze_memory` bars was squeezed and price now closes outside
    the band, enter in the breakout direction; exit when the close crosses
    back through the middle band. Unlike `band_reversion` this bets *with*
    the band break, and only right after compression. Bands come from the
    prior bars only, so a breakout bar can't widen its own threshold.
    """
    warmup = window + squeeze_lookback + 1

    def strategy(history: Sequence[Any], index: int, current_bar: Any) -> float | int | str | None:
        """Hold a breakout that followed a volatility squeeze until price returns to the middle band."""
        if len(history) < warmup:
            return 0
        close = series(history, "close")
        rolling_middle = rolling_mean(close, window)
        rolling_deviation = rolling_std(close, window)
        bandwidth = 2.0 * num_std * rolling_deviation / rolling_middle
        # Strictly below the quantile, so a perfectly steady volatility level doesn't count as a squeeze.
        squeezed_now = (bandwidth < rolling_quantile(bandwidth, squeeze_lookback, squeeze_quantile)).astype(float)
        recently_squeezed = shift(rolling_max(squeezed_now, squeeze_memory)) > 0.0
        middle = shift(rolling_middle)
        deviation = shift(rolling_deviation)
        return latch_position(
            long_entry=recently_squeezed & (close > middle + num_std * deviation),
            long_exit=close < middle,
            short_entry=recently_squeezed & (close < middle - num_std * deviation) if allow_short else None,
            short_exit=close > middle if allow_short else None,
        )

    return strategy


def rsi_reversion_strategy(rsi_window: int = 14, oversold: float = 30.0, exit_level: float = 50.0, allow_short: bool = True) -> StrategyFn:
    """Oscillator mean reversion: buy oversold, hold until the RSI recovers to `exit_level`.

    The overbought level mirrors the oversold one (``100 - oversold``).
    Differs from `band_reversion` in the exit: band_reversion exits the
    moment price is back inside the band (usually before most of the
    reversion has happened), while this holds until momentum has actually
    normalised. Short windows (2-4) with extreme levels (10-20) are the
    classic short-term version.
    """
    warmup = rsi_window + 1
    overbought = 100.0 - oversold
    short_exit_level = 100.0 - exit_level

    def strategy(history: Sequence[Any], index: int, current_bar: Any) -> float | int | str | None:
        """Hold a reversion trade from an RSI extreme until the RSI crosses back to the exit level."""
        if len(history) < warmup:
            return 0
        strength = rsi(series(history, "close"), rsi_window)
        return latch_position(
            long_entry=strength < oversold,
            long_exit=strength > exit_level,
            short_entry=strength > overbought if allow_short else None,
            short_exit=strength < short_exit_level if allow_short else None,
        )

    return strategy


def trend_pullback_strategy(
    trend_window: int = 50,
    rsi_window: int = 3,
    entry_rsi: float = 20.0,
    exit_rsi: float = 70.0,
    allow_short: bool = True,
) -> StrategyFn:
    """Buy short-term dips inside a longer-term uptrend (and sell rallies inside a downtrend).

    A hybrid: the trend filter (close vs its `trend_window` average) decides
    the direction, and a short RSI decides the timing. Enter long when the
    trend is up but the short RSI is oversold, exit when the RSI has bounced
    above `exit_rsi` or the trend filter fails. Mirror image for shorts.
    """
    warmup = max(trend_window, rsi_window) + 1

    def strategy(history: Sequence[Any], index: int, current_bar: Any) -> float | int | str | None:
        """Hold a with-trend pullback entry until the bounce completes or the trend breaks."""
        if len(history) < warmup:
            return 0
        close = series(history, "close")
        trend = rolling_mean(close, trend_window)
        strength = rsi(close, rsi_window)
        return latch_position(
            long_entry=(close > trend) & (strength < entry_rsi),
            long_exit=(strength > exit_rsi) | (close < trend),
            short_entry=(close < trend) & (strength > 100.0 - entry_rsi) if allow_short else None,
            short_exit=(strength < 100.0 - exit_rsi) | (close > trend) if allow_short else None,
        )

    return strategy


def latch_position(
    long_entry: np.ndarray,
    long_exit: np.ndarray,
    short_entry: np.ndarray | None = None,
    short_exit: np.ndarray | None = None,
) -> int:
    """Combine entry and exit rules (boolean arrays over the history) into the position to hold now.

    This is how a strategy gets "enter on X, hold until Y" behaviour while
    still being a pure function of history: you are long if the most recent
    long entry happened after the most recent long exit (same for short).
    An entry on one side also counts as an exit for the other side, and an
    exit wins a same-bar tie with its entry. Because it is recomputed from
    history every bar, it survives restarts and never has hidden state.

    Returns 1 (long), -1 (short) or 0 (flat) for the last bar.
    """
    has_short_side = short_entry is not None and short_exit is not None
    effective_long_exit = long_exit | short_entry if has_short_side else long_exit
    last_long_entry = _last_true_index(long_entry)
    if last_long_entry >= 0 and last_long_entry > _last_true_index(effective_long_exit):
        return 1
    if has_short_side:
        last_short_entry = _last_true_index(short_entry)
        if last_short_entry >= 0 and last_short_entry > _last_true_index(short_exit | long_entry):
            return -1
    return 0


def _last_true_index(mask: np.ndarray) -> int:
    indices = np.flatnonzero(mask)
    return int(indices[-1]) if indices.size else -1


def make_long_only(strategy_fn: StrategyFn) -> StrategyFn:
    """Wrap any strategy so short signals (-1) are suppressed to flat (0).

    Lets any existing strategy be tested as a long-only variant without
    duplicating its signal logic - e.g. to check whether a symmetric
    strategy's shorts are actually adding value or just adding noise.
    """

    def strategy(history: Sequence[Any], index: int, current_bar: Any) -> float | int | str | None:
        """Pass through the wrapped strategy's signal, flattening any short."""
        signal = _normalize_signal(strategy_fn(history, index, current_bar))
        return signal if signal > 0 else 0

    return strategy


def make_regime_gated(
    strategy_fn: StrategyFn,
    *,
    required_regime: str = "trending",
    regime_window: int = 20,
    trending_threshold: float = 0.3,
) -> StrategyFn:
    """Wrap a strategy so it only signals while the market is in `required_regime`.

    Uses `classify_regime` (efficiency-ratio based) on the same bar history
    the strategy already receives, so momentum-family strategies can be
    restricted to trending stretches (or a reversion strategy to ranging
    ones) without a separate data feed.
    """
    from src.signals.regime import classify_regime

    def strategy(history: Sequence[Any], index: int, current_bar: Any) -> float | int | str | None:
        """Pass through the wrapped strategy's signal only in the required regime."""
        regime = classify_regime(history, window=regime_window, trending_threshold=trending_threshold)
        if regime != required_regime:
            return 0
        return strategy_fn(history, index, current_bar)

    return strategy
