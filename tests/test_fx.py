"""Tests for the FX rate collector."""

from __future__ import annotations

from datetime import date

import pytest
from src.data.fx import FXRateCollector


def test_fx_collector_uses_fallback_when_upstream_fails(tmp_path) -> None:
    """The collector should return the configured fallback when upstream fetch fails."""
    collector = FXRateCollector(cache_path=tmp_path / "fx.db")
    collector._request_json = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("upstream unavailable"))  # type: ignore[method-assign]

    rate = collector.get_rate(pair="EUR/NOK", at=date(2024, 1, 6))

    assert rate == 11.5


def test_fx_collector_fetches_daily_norges_bank_rate_for_specific_date(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The collector should fetch and cache a specific daily Norges Bank rate."""
    collector = FXRateCollector(cache_path=tmp_path / "fx.db")

    def fake_request_json(self: FXRateCollector, url: str, *, params=None):
        assert params == {"format": "sdmx-json", "startPeriod": "2024-01-02", "endPeriod": "2024-01-02"}
        return {
            "data": {
                "dataSets": [
                    {
                        "series": {
                            "0:0:0:0": {
                                "observations": {
                                    "0": ["11.2815"],
                                }
                            }
                        }
                    }
                ]
            }
        }

    monkeypatch.setattr(FXRateCollector, "_request_json", fake_request_json)

    rate = collector.get_rate(pair="EUR/NOK", at=date(2024, 1, 2))
    cached_rate = collector.get_rate(pair="EUR/NOK", at=date(2024, 1, 2))

    assert rate == 11.2815
    assert cached_rate == 11.2815


def test_fx_collector_uses_previous_business_day_when_target_has_no_observation(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Weekend dates should fall back to the last preceding business day."""
    collector = FXRateCollector(cache_path=tmp_path / "fx.db")
    requested_dates: list[str] = []

    def fake_request_json(self: FXRateCollector, url: str, *, params=None):
        requested_dates.append(params["startPeriod"])
        if params["startPeriod"] == "2024-01-05":
            return {
                "data": {
                    "dataSets": [
                        {
                            "series": {
                                "0:0:0:0": {
                                    "observations": {
                                        "0": ["11.2000"],
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        return {"data": {"dataSets": [{"series": {"0:0:0:0": {"observations": {}}}}]}}

    monkeypatch.setattr(FXRateCollector, "_request_json", fake_request_json)

    rate = collector.get_rate(pair="EUR/NOK", at=date(2024, 1, 6))

    assert rate == 11.2
    assert requested_dates[:2] == ["2024-01-06", "2024-01-05"]
