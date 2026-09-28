"""Look-ahead tests for the H1-H3 signals on the cached research data (skipped when the data isn't downloaded).

(a) Shifting a signal forward by one bar must change the result (the engine only rewards information it had).
(b) A signal at decision time T must be identical when every input is cut off at T (no row available after T is used).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.research import pit

try:
    pit.load_bars("BTC", "1h")
    HAVE_DATA = True
except FileNotFoundError:
    HAVE_DATA = False

needs_data = pytest.mark.skipif(not HAVE_DATA, reason="hypothesis research data not downloaded (pit.update_hypothesis_data)")
SIGNAL_COLUMNS_H1 = ["F", "F_ann", "z", "dOI3", "basis_ann", "basis_z"]


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("CQM_TRIAL_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.delenv("CQM_FINAL_HOLDOUT", raising=False)


@needs_data
@pytest.mark.parametrize("cutoff", ["2022-06-15", "2024-03-01"])
def test_h1_signal_at_t_uses_only_data_available_by_t(cutoff: str) -> None:
    from src.research.hypotheses.h1_crowding import build_signals

    decision = pd.Timestamp(cutoff, tz="UTC")
    full = build_signals("BTC")
    cut = build_signals("BTC", end=decision)
    assert cut.index.max() <= decision
    pd.testing.assert_series_equal(full.loc[decision, SIGNAL_COLUMNS_H1].astype(float), cut.loc[decision, SIGNAL_COLUMNS_H1].astype(float), check_names=False)


@needs_data
def test_h1_tilt_changes_when_signal_shifted_one_day() -> None:
    from src.research.costs import VENUES
    from src.research.hypotheses.h1_crowding import build_signals, prepare_market, run_tilt

    signals = build_signals("ETH")
    market = prepare_market("ETH", signals, VENUES)
    real = run_tilt(signals, market, source="z", min_hold=3)["overlay"].net.sum()
    shifted = signals.copy()
    shifted[["z", "dOI3"]] = shifted[["z", "dOI3"]].shift(-1)  # tomorrow's signal today: look-ahead
    peek = run_tilt(shifted, market, source="z", min_hold=3)["overlay"].net.sum()
    assert not np.isclose(real, peek)


@needs_data
def test_holdout_is_not_returned_by_research_loaders() -> None:
    for frame in (pit.load_bars("BTC", "1h"), pit.load_funding("ETH", "bybit"), pit.load_open_interest("BTC", "binance"), pit.load_dvol("BTC")):
        assert pd.to_datetime(frame["available_at"], utc=True).max() < pit.holdout_start()
    with pytest.raises(pit.HoldoutLocked):
        pit.load_bars("BTC", "1h", end="2026-02-01")


@needs_data
def test_h3_inputs_at_t_use_only_data_available_by_t() -> None:
    from src.research.hypotheses.h3_vrp import vrp_frame

    decision = pd.Timestamp("2023-05-10", tz="UTC")
    known = ["iv", "rv_trail", "spread", "rv_over_iv", "dvol_ratio_30d"]
    full, cut = vrp_frame("BTC"), vrp_frame("BTC", end=decision)
    pd.testing.assert_series_equal(full.loc[decision, known].astype(float), cut.loc[decision, known].astype(float), check_names=False)
    assert np.isnan(cut.loc[decision, "rv_fwd"])  # the outcome can't be computed from data available at T


@needs_data
def test_h3_sleeve_sizing_uses_only_past_data_and_lag_matters() -> None:
    from src.research.costs import VENUES
    from src.research.hypotheses.h3_vrp import prepare_sleeves, sleeve_run, vrp_frame

    decision = pd.Timestamp("2024-02-01", tz="UTC")
    full = prepare_sleeves("ETH", vrp_frame("ETH"), VENUES["kraken_perp"])[0]
    cut = prepare_sleeves("ETH", vrp_frame("ETH", end=decision), VENUES["kraken_perp"], end=decision)[0]
    last = cut.bars.index[-1]
    for name in ("signal", "ewma", "iv", "rv_over_iv"):
        assert np.isclose(getattr(full, name).loc[last], getattr(cut, name).loc[last], equal_nan=True), name
    real = sleeve_run(full, "iv").net.sum()
    peek = sleeve_run(type(full)(**{**{f: getattr(full, f) for f in full.__slots__}, "signal": full.signal.shift(-1).fillna(0.0)}), "iv").net.sum()
    assert not np.isclose(real, peek)
