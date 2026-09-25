"""Tests for historical OHLCV fetching helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.data.historical import _normalize_pair_code, fetch_kraken_ohlcv, fetch_kraken_ohlcv_history, load_or_fetch_kraken_history
from src.storage.bar_aggregator import OHLCVBar


def test_normalize_pair_code_supports_common_symbols() -> None:
    """Common Kraken symbols should map to Kraken pair identifiers."""
    assert _normalize_pair_code("BTC/EUR") == "XXBTZEUR"
    assert _normalize_pair_code("ETH/USD") == "XETHZUSD"


def test_fetch_kraken_ohlcv_parses_rows() -> None:
    """The historical fetcher should parse Kraken OHLC rows into OHLCV bars."""

    class FakeResponse:
        """Represent a FakeResponse."""
        def __init__(self, payload: str) -> None:
            """Initialize the object with its runtime state."""
            self._payload = payload.encode("utf-8")

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            """Perform the read operation."""
            return self._payload

    payload = {
        "error": [],
        "result": {
            "XXBTZEUR": [
                [1710000000, 100.0, 101.0, 99.5, 100.5, 100.2, 10.0, 1],
            ]
        },
    }

    with patch(
        "src.data.historical.urllib.request.urlopen",
        side_effect=lambda request, timeout=10: FakeResponse(
            '{"error": [], "result": {"XXBTZEUR": [[1710000000, 100.0, 101.0, 99.5, 100.5, 100.2, 10.0, 1]]}}'
        ),
    ):
        bars = fetch_kraken_ohlcv(symbol="BTC/EUR", interval_seconds=60, count=1)

    assert len(bars) == 1
    assert bars[0].close == 100.5
    assert bars[0].volume == 10.0


def _recent_bar(reference: datetime, hours_ago: float) -> OHLCVBar:
    return OHLCVBar(
        exchange="kraken",
        symbol="BTC/EUR",
        interval_seconds=3600,
        timestamp=reference - timedelta(hours=hours_ago),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0 + (10 - hours_ago),
        volume=10.0,
    )


def test_fetch_kraken_ohlcv_history_paginates_until_caught_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """The paginated fetcher should chain calls via `since` and stop once it catches up to now."""
    now = datetime.now(timezone.utc)
    pages = [
        [_recent_bar(now, 3.0), _recent_bar(now, 2.0), _recent_bar(now, 1.0)],
        [_recent_bar(now, 1.0), _recent_bar(now, 0.1)],  # overlapping boundary bar, should be deduplicated
    ]

    def fake_fetch(*, symbol, interval_seconds, count, since=None):
        return pages.pop(0) if pages else []

    monkeypatch.setattr("src.data.historical.fetch_kraken_ohlcv", fake_fetch)

    bars = fetch_kraken_ohlcv_history(symbol="BTC/EUR", interval_seconds=3600, lookback_days=1, request_pause_seconds=0.0)

    assert len(bars) == 4
    assert bars == sorted(bars, key=lambda bar: bar.timestamp)


def test_load_or_fetch_kraken_history_uses_cache_when_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cached parquet file with recent-enough data should be reused instead of re-fetching."""
    call_count = {"n": 0}

    def fake_fetch_history(*, symbol, interval_seconds, lookback_days):
        call_count["n"] += 1
        now = datetime.now(timezone.utc)
        # Spans close to the full requested lookback (~1 day), not just a
        # single recent bar, so the cache-depth check accepts it as complete.
        return [
            OHLCVBar(
                exchange="kraken",
                symbol=symbol,
                interval_seconds=interval_seconds,
                timestamp=now - timedelta(hours=hours_ago),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=10.0,
            )
            for hours_ago in (24, 12, 1)
        ]

    monkeypatch.setattr("src.data.historical.fetch_kraken_ohlcv_history", fake_fetch_history)

    first = load_or_fetch_kraken_history(symbol="BTC/EUR", interval_seconds=3600, lookback_days=1, cache_dir=tmp_path)
    second = load_or_fetch_kraken_history(symbol="BTC/EUR", interval_seconds=3600, lookback_days=1, cache_dir=tmp_path)

    assert call_count["n"] == 1  # second call served from cache
    assert len(first) == 3
    # The boundary bar may or may not clear the rolling cutoff depending on
    # exact test timing; what matters is the cache was reused, not re-fetched.
    assert len(second) >= 2


def test_load_or_fetch_kraken_history_refetches_when_cache_is_too_shallow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache built for a short lookback must not be silently reused to satisfy a much longer request."""
    call_count = {"n": 0}

    def fake_fetch_history(*, symbol, interval_seconds, lookback_days):
        call_count["n"] += 1
        now = datetime.now(timezone.utc)
        # Only ever returns bars spanning the last day, regardless of what
        # lookback_days was requested - simulates a real short history.
        return [
            OHLCVBar(
                exchange="kraken",
                symbol=symbol,
                interval_seconds=interval_seconds,
                timestamp=now - timedelta(hours=hours_ago),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=10.0,
            )
            for hours_ago in (24, 12, 0)
        ]

    monkeypatch.setattr("src.data.historical.fetch_kraken_ohlcv_history", fake_fetch_history)

    load_or_fetch_kraken_history(symbol="BTC/EUR", interval_seconds=3600, lookback_days=1, cache_dir=tmp_path)
    assert call_count["n"] == 1

    # Asking for a much deeper history than the cache actually covers should
    # trigger a fresh fetch, not silently return the shallow cached data.
    load_or_fetch_kraken_history(symbol="BTC/EUR", interval_seconds=3600, lookback_days=90, cache_dir=tmp_path)
    assert call_count["n"] == 2
