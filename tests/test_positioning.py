"""Tests for public positioning data: paging, caching, and alignment without look-ahead."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from src.data import positioning
from src.data.positioning import align_to_bars, fetch_binance_funding, fetch_bybit_funding, load_series

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_alignment_only_uses_values_published_by_the_bar_close() -> None:
    """A funding print at 08:00 is visible to the bar closing at 08:00, not to the one closing at 07:00."""
    series = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01 00:00", "2024-01-01 08:00"], utc=True), "binance_funding": [0.0001, 0.0005]})
    closes = pd.date_range("2024-01-01 07:00", periods=3, freq="h", tz="UTC")
    aligned = align_to_bars(series, closes)
    assert list(aligned["binance_funding"]) == [0.0001, 0.0005, 0.0005]


def test_bars_stamped_at_open_count_as_published_at_their_close() -> None:
    """A Binance 1h candle opening 07:00 closes 08:00, so it belongs to the bar closing at 08:00, not 07:00."""
    klines = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01 06:00", "2024-01-01 07:00"], utc=True), "binance_volume": [10.0, 20.0]})
    closes = pd.DatetimeIndex(pd.to_datetime(["2024-01-01 07:00", "2024-01-01 08:00"], utc=True))
    aligned = align_to_bars(klines, closes, stamped_at_open=timedelta(hours=1))
    assert list(aligned["binance_volume"]) == [10.0, 20.0]


def test_binance_funding_pages_forward_until_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pages of 1000 continue from the last settlement time until a short page."""
    calls: list[int] = []

    def fake(url, params=None, retries=3):
        calls.append(params["startTime"])
        start = params["startTime"]
        size = 1000 if len(calls) == 1 else 3
        return [{"fundingTime": start + i * 8 * 3600 * 1000, "fundingRate": "0.0001"} for i in range(size)]

    monkeypatch.setattr(positioning, "_get_json", fake)
    frame = fetch_binance_funding("BTCUSDT", T0, T0 + timedelta(days=400))
    assert len(calls) == 2 and len(frame) == 1003
    assert frame["timestamp"].is_monotonic_increasing


def test_bybit_funding_pages_backwards(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bybit returns newest first; paging moves endTime back past the oldest row."""
    def fake(url, params=None, retries=3):
        end = params["endTime"]
        size = 200 if end > positioning._ms(T0 + timedelta(days=60)) else 5
        return {"result": {"list": [{"fundingRateTimestamp": end - i * 8 * 3600 * 1000, "fundingRate": "0.0002"} for i in range(size)]}}

    monkeypatch.setattr(positioning, "_get_json", fake)
    frame = fetch_bybit_funding("BTCUSDT", T0, T0 + timedelta(days=90))
    assert len(frame) == 205 and frame["bybit_funding"].eq(0.0002).all()


def test_load_series_caches_and_only_fetches_what_is_new(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The second load reuses the parquet cache and asks only for rows after the last cached one."""
    requested: list[tuple[datetime, datetime]] = []

    def fake_fetch(symbol, start, end):
        requested.append((start, end))
        stamps = pd.date_range(start, end, freq="8h", tz="UTC")
        return pd.DataFrame({"timestamp": stamps, "binance_funding": 0.0001})

    monkeypatch.setitem(positioning.SOURCES, "binance_funding", (fake_fetch, "binance", datetime.now(timezone.utc) - timedelta(days=10)))
    first = load_series("binance_funding", "BTC", cache_dir=tmp_path)
    second = load_series("binance_funding", "BTC", cache_dir=tmp_path)
    assert len(first) >= 29 and len(second) >= len(first)
    assert len(requested) <= 2 and (len(requested) == 1 or requested[1][0] > first["timestamp"].max().to_pydatetime())
    with pytest.raises(ValueError):
        load_series("binance_funding", "DOGE", cache_dir=tmp_path)
