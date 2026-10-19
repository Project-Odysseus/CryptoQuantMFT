"""The host runs a quantlab strategy spec with the research code (plan 7.1): same signal, version pinned."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.backtest.strategies import trend_forecast_strategy
from src.research.catalog import build_strategy
from src.storage.bar_aggregator import OHLCVBar


def _bars(count: int = 400) -> list[OHLCVBar]:
    rng = np.random.default_rng(0)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, count)))
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    return [OHLCVBar("cme", "MES", 86400, start + timedelta(days=i), close[i], close[i] * 1.005, close[i] * 0.995, close[i], 1.0) for i in range(count)]


def test_the_smooth_trend_spec_gives_exactly_the_host_trend_forecast() -> None:
    bars = _bars()
    lab = build_strategy("lab", spec="smooth_trend", version=1)
    host = trend_forecast_strategy("compound_sigmoid", warmup=0)
    np.testing.assert_allclose(lab.signal_series(bars)[200:], host.signal_series(bars)[200:], atol=1e-12)
    assert lab.spec.key == "smooth_trend@v1"


def test_a_sleeve_pinned_to_another_version_is_refused() -> None:
    with pytest.raises(ValueError, match="promote the new version"):
        build_strategy("lab", spec="smooth_trend", version=2)


def test_portfolio_check_requires_a_promotion_with_the_same_hash(tmp_path, monkeypatch, capsys) -> None:
    import main
    from quantlab.deploy.spec import find_spec, load_spec
    from quantlab.validation import ledger
    from src.portfolio.config import load_portfolio_config

    monkeypatch.setenv("QUANTLAB_LEDGER", str(tmp_path / "ledger.jsonl"))
    text = open("config/portfolio.equity_trend_paper.toml").read().replace(
        'strategy = "trend_forecast"\nparams = { activation = "compound_sigmoid" }', 'strategy = "lab"\nparams = { spec = "smooth_trend", version = 1 }')
    path = tmp_path / "book.toml"
    path.write_text(text)
    config = load_portfolio_config(path)
    assert main.lab_promotion_status(config) == ["mnq_forecast", "mes_forecast", "fxxp_forecast"]
    spec = load_spec(find_spec("smooth_trend"))
    ledger.append({"type": "promotion", "spec": spec.key, "id": spec.id, "version": 1, "hash": "an older file", "stage": "paper", "approved_by": "owner"})
    assert len(main.lab_promotion_status(config)) == 3 and "changed after its promotion" in capsys.readouterr().out
    ledger.append({"type": "promotion", "spec": spec.key, "id": spec.id, "version": 1, "hash": spec.hash, "stage": "paper", "approved_by": "owner",
                   "overrides": {"cards": "H13 already on paper"}})
    assert main.lab_promotion_status(config) == []
    assert main.portfolio_check(str(path), require_cards=True) == 0  # lab sleeves need a promotion, not a host card
