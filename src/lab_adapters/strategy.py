"""Run a quantlab strategy spec as a host strategy, so paper and live books trade the research code (plan 7.1).

A portfolio sleeve names the spec and the version it was promoted at:

    [[sleeves]]
    strategy = "lab"
    params = { spec = "smooth_trend", version = 1 }
    continuous = true

The spec file (research/strategies/<spec>.toml) is loaded once; its signal is the same quantlab function research
backtested, applied to the sleeve's bars. A spec whose file now has another version than the sleeve pins is refused,
so an edited spec cannot reach a book without a new promotion (`--portfolio-check --require-strategy-cards` also checks
that the pinned version was promoted, with the same hash).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from src.backtest.indicators import series


def bar_dates(bars: Sequence[Any]) -> Any:
    """Each host bar's session date."""
    import pandas as pd

    times = [bar["timestamp"] if isinstance(bar, dict) else bar.timestamp for bar in bars]
    return pd.DatetimeIndex(pd.to_datetime(times, utc=True)).tz_convert(None).normalize()


def bars_frame(bars: Sequence[Any]) -> Any:
    """Host bars (objects or dicts) as the lab's bar frame, indexed by session date (the last bar of a date wins)."""
    import pandas as pd

    frame = pd.DataFrame({field: series(bars, field) for field in ("open", "high", "low", "close", "volume")}, index=bar_dates(bars))
    frame.index.name = "time"
    return frame[~frame.index.duplicated(keep="last")]


def lab_strategy(spec: str, version: int) -> Any:
    """A host strategy (with `signal_series`) computing the spec's forecast; `version` must match the file."""
    from quantlab.deploy.spec import find_spec, load_spec

    loaded = load_spec(find_spec(spec))
    if loaded.version != int(version):
        raise ValueError(f"strategy spec {spec!r} is at version {loaded.version}, the sleeve pins version {version}: promote the new version first")
    signal = loaded.signal_fn()

    def signal_series(bars: Sequence[Any]) -> np.ndarray:
        if not len(bars):
            return np.array([])
        values = signal(bars_frame(bars)).astype(float).clip(-2.0, 2.0)
        return np.nan_to_num(values.reindex(bar_dates(bars)).to_numpy())  # one value per host bar, by its date

    def strategy(history: Sequence[Any], index: int, current_bar: Any) -> float:
        values = signal_series(history)
        return float(values[-1]) if len(values) else 0.0

    strategy.signal_series = signal_series  # type: ignore[attr-defined]
    strategy.spec = loaded  # type: ignore[attr-defined]
    return strategy
