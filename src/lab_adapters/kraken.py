"""Kraken Futures perp history (the host's cached candles) as a quantlab data adapter.

Symbols are the host's ("BTC/USD"). The host's candles are stamped at their open; the adapter re-stamps them by
their close, the lab's convention, and daily candles by their UTC date. Nothing is downloaded when the host cache
already covers the range (the host loader's behaviour).
"""

from __future__ import annotations

import pandas as pd

from quantlab.data.bars import from_open_stamped


class KrakenAdapter:
    """Kraken Futures perpetuals: daily and 4h bars from the host cache (BTC and ETH stitched from 2020)."""

    name, version = "kraken", "1"

    def __init__(self, loader=None) -> None:
        self._loader = loader

    def _bars(self, symbol: str, interval: str) -> list:
        if self._loader is not None:
            return self._loader(symbol, interval)
        from src.portfolio.backtest import default_bar_loader
        from src.portfolio.config import InstrumentSpec

        return default_bar_loader(InstrumentSpec(id=f"kraken_futures:{symbol}", kind="perp"), interval)

    def fetch(self, symbol: str, *, interval: str = "1d", start: str | None = None, end: str | None = None) -> pd.DataFrame:
        bars = self._bars(symbol, interval)
        frame = pd.DataFrame({"open": [b.open for b in bars], "high": [b.high for b in bars], "low": [b.low for b in bars],
                              "close": [b.close for b in bars], "volume": [b.volume for b in bars]},
                             index=pd.DatetimeIndex([b.timestamp for b in bars]), dtype=float)
        frame = from_open_stamped(frame[~frame.index.duplicated(keep="last")].sort_index(), interval)
        return frame.loc[start:end] if (start or end) else frame
