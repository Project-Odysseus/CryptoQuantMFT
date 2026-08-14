"""Tests for historical OHLCV fetching helpers."""

from __future__ import annotations

from unittest.mock import patch

from src.data.historical import _normalize_pair_code, fetch_kraken_ohlcv


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
