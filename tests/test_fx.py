"""Tests for the FX rate collector."""

from __future__ import annotations

from pathlib import Path

from src.data.fx import FXRateCollector


def test_fx_collector_uses_fallback_when_upstream_fails(tmp_path: Path) -> None:
    """The collector should return the configured fallback when live fetch fails."""
    collector = FXRateCollector(cache_path=tmp_path / "fx.db")

    rate = collector.get_rate(pair="EUR/NOK")

    assert rate == 11.5
