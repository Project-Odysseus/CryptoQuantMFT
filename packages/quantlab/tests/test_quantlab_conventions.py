"""The lab's conventions as code (quantlab.conventions)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from quantlab import conventions as cv


def test_periods_per_year() -> None:
    assert cv.periods_per_year("1d") == 252
    assert cv.periods_per_year("1d", "always") == 365
    assert cv.periods_per_year("4h", "always") == 365 * 6
    assert cv.periods_per_year("1h", cv.Calendar.EXCHANGE) == pytest.approx(252 * 6.5)
    with pytest.raises(ValueError):
        cv.periods_per_year("2d")


def test_returns_round_trip() -> None:
    prices = pd.Series([100.0, 110.0, 99.0])
    simple, logs = cv.simple_returns(prices), cv.log_returns(prices)
    assert simple.iloc[1] == pytest.approx(0.10) and logs.iloc[1] == pytest.approx(math.log(1.1))
    assert np.allclose(cv.to_simple(logs.iloc[1:]), simple.iloc[1:]) and np.allclose(cv.to_log(simple.iloc[1:]), logs.iloc[1:])


def test_excess_return_of_an_index_proxy() -> None:
    flat = pd.Series([0.0, 0.0])
    assert np.allclose(cv.excess_return(flat, cash_rate=0.04, carry=0.02), -0.02 / 252)


def test_weights_and_contracts_convert_both_ways() -> None:
    contracts = cv.weight_to_contracts(0.33, equity=10_000_000, price=7744.5, multiplier=5, fx=10.8)
    assert contracts == pytest.approx(0.33 * 10_000_000 / (7744.5 * 5 * 10.8))
    assert cv.contracts_to_weight(contracts, equity=10_000_000, price=7744.5, multiplier=5, fx=10.8) == pytest.approx(0.33)
    with pytest.raises(ValueError):
        cv.weight_to_contracts(0.1, equity=0, price=1, multiplier=1)


def test_forecast_cap_and_regime_probabilities() -> None:
    assert list(cv.cap_forecast(np.array([-3.0, 0.5, 2.5]))) == [-2.0, 0.5, 2.0]
    good = pd.DataFrame({"trend": [0.7, np.nan], "range": [0.3, np.nan]})
    cv.check_regime_probabilities(good)
    with pytest.raises(ValueError, match="sum to 1"):
        cv.check_regime_probabilities(pd.DataFrame({"trend": [0.7], "range": [0.4]}))
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        cv.check_regime_probabilities(pd.DataFrame({"trend": [1.2], "range": [-0.2]}))
