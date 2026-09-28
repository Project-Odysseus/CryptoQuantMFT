"""H3, variance risk premium, phase A: IV vs realized vol, and IV-based sizing of the live book's sleeves.

Definitions follow research/prereg/H3.txt. DVOL comes from `pit.load_dvol` (a candle's close is available an hour
after its open stamp), realized vol from Binance perp 5-minute bars, and the sleeves are the live book's strategies
from the registry, run on Binance perp bars resampled from 1h.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.research import pit
from src.research.bar_engine import TargetRun, run_targets
from src.research.costs import VenueCosts
from src.research.hypotheses.common import cost_at, resample_bars

RV_DAYS = 30
MIN_COVERAGE = 0.9
TARGET_VOL, MAX_LEVERAGE, HALFLIFE_DAYS = 0.5, 2.0, 10.0
SLEEVES = (
    {"id": "ma_1d", "strategy": "moving_average_crossover", "params": {"short_window": 4, "long_window": 48}, "long_only": True, "rule": "1D"},
    {"id": "keltner_ls_1d", "strategy": "keltner_breakout", "params": {"window": 40, "atr_multiplier": 2.0}, "long_only": False, "rule": "1D"},
    {"id": "ma_4h", "strategy": "moving_average_crossover", "params": {"short_window": 8, "long_window": 96}, "long_only": True, "rule": "4h"},
)


def realized_vol_frame(coin: str, decisions: pd.DatetimeIndex, *, end: Any = None) -> pd.DataFrame:
    """Trailing and forward 30-day realized vol (annualised) at each decision time, from 5-minute log returns.

    Forward values are outcomes and are NaN when fewer than 90% of the window's bars exist (e.g. past the data).
    """
    bars = pit.load_bars(coin, "5m", end=end)
    close = bars["close"]
    returns = np.log(close / close.shift(1))
    consecutive = bars.index.to_series().diff() == pd.Timedelta(minutes=5)
    squared = (returns.where(consecutive) ** 2).fillna(0.0)
    counted = consecutive.astype(float)
    times = pd.DatetimeIndex(bars["available_at"]).asi8
    cs, cn = np.concatenate([[0.0], np.cumsum(squared.to_numpy())]), np.concatenate([[0.0], np.cumsum(counted.to_numpy())])
    span = pd.Timedelta(days=RV_DAYS)
    expected = RV_DAYS * 288

    def window(lo: pd.DatetimeIndex, hi: pd.DatetimeIndex) -> np.ndarray:
        a, b = np.searchsorted(times, lo.asi8, side="right"), np.searchsorted(times, hi.asi8, side="right")
        total, count = cs[b] - cs[a], cn[b] - cn[a]
        with np.errstate(invalid="ignore", divide="ignore"):
            vol = np.sqrt(total * 365.0 / RV_DAYS)
        return np.where(count >= MIN_COVERAGE * expected, vol, np.nan)

    return pd.DataFrame({"rv_trail": window(decisions - span, decisions), "rv_fwd": window(decisions, decisions + span)}, index=decisions)


def vrp_frame(coin: str, *, end: Any = None) -> pd.DataFrame:
    """Daily IV, trailing and forward RV, the premium, and the IV-RV spread at each 00:00 UTC decision."""
    dvol = pit.load_dvol(coin, end=end)
    first, last = dvol["available_at"].min().ceil("D"), dvol["available_at"].max().floor("D")
    decisions = pd.date_range(first, last, freq="D", tz="UTC")
    frame = pit.asof(dvol, decisions, ["dvol"]).rename(columns={"dvol": "iv"}) / 100.0
    frame = frame.join(realized_vol_frame(coin, decisions, end=end))
    frame["vrp"] = frame["iv"] - frame["rv_fwd"]
    frame["vrp_var"] = frame["iv"] ** 2 - frame["rv_fwd"] ** 2
    frame["spread"] = frame["iv"] - frame["rv_trail"]
    # the debiasing factor: median realized-over-implied over the trailing year, known at T
    frame["rv_over_iv"] = (frame["rv_trail"] / frame["iv"]).rolling(365, min_periods=180).median()
    frame["dvol_ratio_30d"] = frame["iv"] / frame["iv"].rolling(30, min_periods=20).mean()
    return frame


def to_bar_objects(bars: pd.DataFrame, symbol: str) -> list[Any]:
    """OHLCVBar objects for the registry strategies' `signal_series`."""
    from src.storage.bar_aggregator import OHLCVBar

    seconds = int((bars.index[1] - bars.index[0]).total_seconds())
    return [OHLCVBar(exchange="binance_futures", symbol=symbol, interval_seconds=seconds, timestamp=t.to_pydatetime(), open=float(r.open),
                     high=float(r.high), low=float(r.low), close=float(r.close), volume=float(r.volume)) for t, r in zip(bars.index, bars.itertuples())]


def ewma_forecast(close: pd.Series, bar_hours: float) -> pd.Series:
    """The runtime's VolatilityTargetSizer forecast at every bar close: zero-mean EWMA of log returns, half-life 10 days,
    truncated after eight half-lives, annualised; NaN with fewer than 20 returns."""
    halflife = max(1.0, HALFLIFE_DAYS * 24.0 / bar_hours)
    squared = np.log(close / close.shift(1)) ** 2
    window = int(8 * halflife) + 1
    weights = 0.5 ** (np.arange(window)[::-1] / halflife)
    values = squared.to_numpy()
    out = np.full(len(values), np.nan)
    for i in range(20, len(values)):
        chunk = values[max(1, i - window + 1): i + 1]
        w = weights[-len(chunk):]
        out[i] = np.sqrt(np.sum(w * chunk) / np.sum(w) * 365.0 * 24.0 / bar_hours)
    return pd.Series(out, index=close.index)


def entry_only_weights(signal: pd.Series, sigma: pd.Series) -> pd.Series:
    """Weight = signal * min(target / sigma at entry, max leverage), fixed until the signal changes (as the runtime)."""
    segment = (signal != signal.shift()).cumsum()
    at_entry = sigma.groupby(segment).transform("first")
    size = (TARGET_VOL / at_entry).clip(upper=MAX_LEVERAGE)
    return (signal * size).where(signal != 0, 0.0).fillna(0.0)


@dataclass(frozen=True, slots=True)
class SleeveData:
    """One sleeve on one coin: bars, raw signal, and the per-bar inputs every sizing needs (all known at bar close)."""

    coin: str
    sleeve: str
    bars: pd.DataFrame
    signal: pd.Series
    ewma: pd.Series
    iv: pd.Series
    rv_over_iv: pd.Series
    dvol_ratio: pd.Series
    cost: np.ndarray  # per side at each bar's open (where a fill decided at the previous close happens)
    funding: pd.DataFrame


def prepare_sleeves(coin: str, vrp: pd.DataFrame, venue: VenueCosts, *, end: Any = None) -> list[SleeveData]:
    """Every sleeve's bars and inputs for `coin`, from Binance perp 1h resampled to the sleeve's interval."""
    from src.research.catalog import build_strategy

    hourly = pit.load_bars(coin, "1h", end=end)
    dvol = pit.load_dvol(coin, end=end)
    funding = pit.load_funding(coin, "binance", end=end)
    out = []
    for spec in SLEEVES:
        bars = resample_bars(hourly, spec["rule"])
        hours = (bars.index[1] - bars.index[0]).total_seconds() / 3600
        closes_at = bars.index + pd.Timedelta(hours=hours)  # decision time = bar close
        strategy = build_strategy(spec["strategy"], **spec["params"], long_only=spec["long_only"])
        signal = pd.Series(np.sign(np.nan_to_num(np.asarray(strategy.signal_series(to_bar_objects(bars, f"{coin}USDT")), dtype=float))), index=bars.index)
        iv = pd.Series(pit.asof(dvol, closes_at, ["dvol"])["dvol"].to_numpy() / 100.0, index=bars.index)
        daily = vrp[["rv_over_iv", "dvol_ratio_30d"]].copy()
        daily["available_at"] = daily.index
        known = pit.asof(daily, closes_at, ["rv_over_iv", "dvol_ratio_30d"])
        out.append(SleeveData(coin, spec["id"], bars, signal, ewma_forecast(bars["close"], hours), iv,
                              pd.Series(known["rv_over_iv"].to_numpy(), index=bars.index), pd.Series(known["dvol_ratio_30d"].to_numpy(), index=bars.index),
                              cost_at(hourly, bars.index, venue, coin), funding))  # cost at each fill (a bar's open)
    return out


def sleeve_run(data: SleeveData, sizing: str, *, multiplier: float = 1.0, iv_shift_days: int = 0, risk_off_proxy: bool = False) -> TargetRun:
    """One sleeve with a sizing ("ewma", "iv", "iv_debiased"), optional A2-proxy risk-off, optional IV placebo shift.

    Bars and weights share an index; a weight decided at a bar's close fills at the next bar's open.
    """
    iv = data.iv
    if iv_shift_days:
        bars_per_day = int(round(pd.Timedelta(days=1) / (data.bars.index[1] - data.bars.index[0])))
        iv = pd.Series(np.roll(iv.to_numpy(), iv_shift_days * bars_per_day), index=iv.index)
    sigma = {"ewma": data.ewma, "iv": iv, "iv_debiased": iv * data.rv_over_iv}[sizing]
    signal = data.signal.where(sigma.notna() & data.iv.notna(), 0.0)  # every variant trades the same dates
    if risk_off_proxy:
        signal = signal.where(~(data.dvol_ratio > 1.25), 0.0)
    weights = entry_only_weights(signal, sigma)
    return run_targets(data.bars, weights, cost_per_side=data.cost * multiplier, funding=data.funding)


def daily(values: pd.Series) -> pd.Series:
    """Per-bar simple returns summed into UTC days (4h sleeves -> daily for the book)."""
    return values.groupby(values.index.floor("D")).sum()
