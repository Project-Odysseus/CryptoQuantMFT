"""H14: a trend model and a range model on one equity index, combined by a causal three-state regime overlay.

research/prereg/H14.txt has the rules and what decides the result; research/h14_regime_switch.toml has every number.
The pieces are separate so each can be tested on its own:

1. `load_market` / `market_returns`: daily OHLC of the index (the futures proxy) and what a rolled future earns
   (index move + (dividend yield - local rate) / 252: no roll gaps, no back-adjustment).
2. `regime_scores`: features (src/signals/regime_features.py) as walk-forward percentile scores, the trend
   probability and the stress flag.
3. `trend_model`: normalised moving-average trend plus a 60/20-day breakout state, in [-1, 1].
4. `classify`: the regime state machine (RANGE / TRANSITION / TREND) with confirmation, hysteresis, the rapid-breakout
   exception, and the model weights of each variant.
5. `range_model`: z-score mean reversion around a 40-day EWMA, with boundary vetoes, stops, a maximum holding period
   and no same-bar reversal. Its entries depend on the overlay, so it is run per variant.
6. `combine`: weights, conflict rule, transition cap, stress scaling, dead zone -> the target signal.
7. `backtest`: volatility scaling, the band, execution two closes later, costs, P&L and trade records.

Every value at bar t uses data up to bar t's close. The target decided at t is traded at the close of t+1 and earns
from t+2, so no position ever trades on the close that produced it.
"""

from __future__ import annotations

import asyncio
import math
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

import numpy as np
import pandas as pd

from src.backtest.indicators import atr, ema, rolling_mean
from src.research import governance
from src.signals import regime_features as rf
from src.signals.trend_forecast import ewma_volatility

CONFIG_PATH = Path("research/h14_regime_switch.toml")
CACHE_DIR = Path("data/research_cache/h14")
RANGE, TRANSITION, TREND, NONE = "RANGE", "TRANSITION", "TREND", "NONE"  # NONE: a variant without regimes
STATE_CODE = {RANGE: -1, TRANSITION: 0, TREND: 1}
SCORE_FEATURES = {"er": "er", "slope": "abs_slope_t", "adx": "adx", "vr": "vr", "breakout": "breakout_beyond"}


# --- configuration ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TrendSettings:
    fast_mean: int = 20
    slow_mean: int = 100
    atr: int = 20
    ma_scale: float = 2.0
    breakout_entry: int = 60
    breakout_exit: int = 20
    ma_weight: float = 0.5
    breakout_weight: float = 0.5


@dataclass(frozen=True, slots=True)
class RangeSettings:
    anchor_span: int = 40
    residual_vol: int = 40
    entry_z: float = 1.5
    boundary: int = 40
    boundary_atr: float = 0.5
    signal_scale: float = 2.5
    max_hold: int = 20
    strong_trend: float = 0.5
    vol_veto_ratio: float = 1.5
    regime_entry_weight: float = 0.60
    regime_exit_weight: float = 0.40
    short_cap_b: float = 0.5


@dataclass(frozen=True, slots=True)
class RegimeSettings:
    horizon: int = 40
    adx: int = 14
    vr_lag: int = 5
    short_vol: int = 10
    long_vol: int = 60
    breakout: int = 60
    calibration_days: int = 500
    min_calibration: int = 250
    weights: dict[str, float] = field(default_factory=lambda: {"er": 0.25, "slope": 0.25, "adx": 0.20, "vr": 0.15, "breakout": 0.15})
    trend_entry: float = 0.65
    range_entry: float = 0.35
    exit_threshold: float = 0.50
    confirm: int = 3
    confirm_fast: int = 2
    exit_confirm: int = 2
    direction_median: float = 0.5
    rapid_breakout_atr: float = 0.5
    stress_percentile: float = 0.95
    stress_gap_atr: float = 3.0
    stress_signal_scale: float = 0.5
    stress_range_block: int = 3
    stress_max_step: float = 0.5
    transition_cap: float = 0.50
    dead_zone: float = 0.10


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    trade_lag: int = 2
    target_vol: float = 0.10
    vol_span: int = 20
    max_position: float = 2.0
    rebalance_band: float = 0.02
    rolls_per_year: int = 4
    roll_bps: float = 1.0
    cost_multipliers: dict[str, float] = field(default_factory=lambda: {"low": 0.5, "base": 1.0, "high": 2.5})
    japan_rate_before_source: float = 0.1


@dataclass(frozen=True, slots=True)
class Config:
    """The whole study configuration (research/h14_regime_switch.toml)."""

    version: int
    data: dict[str, Any]
    markets: dict[str, dict[str, Any]]
    execution: ExecutionSettings
    trend: TrendSettings
    range: RangeSettings
    regime: RegimeSettings
    filters: dict[str, Any]
    sensitivity: dict[str, Any]
    episodes: dict[str, list[str]]

    def with_changes(self, **changes: Any) -> "Config":
        """A copy with section settings changed, e.g. regime={"horizon": 30}, trend={"fast_mean": 10}."""
        updated = {name: replace(getattr(self, name), **values) for name, values in changes.items()}
        return replace(self, **updated)


def _section(cls: type, raw: Mapping[str, Any], name: str) -> Any:
    unknown = sorted(set(raw) - {item.name for item in fields(cls)})
    if unknown:
        raise ValueError(f"[{name}] unknown keys {unknown}")
    return cls(**raw)


def load_config(path: Path | str = CONFIG_PATH) -> Config:
    """The study configuration. Unknown keys are errors, so a typo cannot silently fall back to a default."""
    raw = tomllib.loads(Path(path).read_text())
    expected = {"version", "data", "markets", "execution", "trend", "range", "regime", "filters", "sensitivity", "episodes"}
    if set(raw) != expected:
        raise ValueError(f"{path}: sections {sorted(raw)} != {sorted(expected)}")
    return Config(version=int(raw["version"]), data=dict(raw["data"]), markets={key: dict(value) for key, value in raw["markets"].items()},
                  execution=_section(ExecutionSettings, raw["execution"], "execution"), trend=_section(TrendSettings, raw["trend"], "trend"),
                  range=_section(RangeSettings, raw["range"], "range"), regime=_section(RegimeSettings, raw["regime"], "regime"),
                  filters=dict(raw["filters"]), sensitivity=dict(raw["sensitivity"]), episodes={key: list(value) for key, value in raw["episodes"].items()})


# --- data --------------------------------------------------------------------------------------------------------------

def _run(coroutine: Any) -> Any:
    """Run a coroutine to completion, also from inside a running event loop (Jupyter), by using a separate thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


async def _download(symbols: Mapping[str, str], start: str) -> dict[str, pd.DataFrame]:
    import aiohttp

    from src.futures.quotes import parse_chart
    from src.research import multi_asset as ma

    first, last = int(pd.Timestamp(start).timestamp()), int(pd.Timestamp.now(tz="UTC").timestamp())
    async with aiohttp.ClientSession() as session:
        payloads = await asyncio.gather(*(ma._get(session, ma._YAHOO.format(symbol=quote(symbol), start=first, end=last), json=True) for symbol in symbols.values()))
    return {market: parse_chart(payload)[0] for market, payload in zip(symbols, payloads)}


def load_market(config: Config, market: str, *, refresh: bool = False, cache_dir: Path | str = CACHE_DIR) -> pd.DataFrame:
    """Daily OHLCV of one market from the cache (downloaded once), without the frozen final holdout."""
    path = Path(cache_dir) / f"{market}.csv"
    if refresh or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = _run(_download({market: config.markets[market]["symbol"]}, config.data["download_start"]))[market]
        frame.to_csv(path)
    frame = pd.read_csv(path, index_col=0, parse_dates=True)
    frame.index.name = "day"
    frame = frame[~frame.index.duplicated(keep="last")].sort_index().dropna(subset=["close"])
    return governance.trim_frame(frame, label=f"H14 {market}")


def data_checks(frame: pd.DataFrame) -> dict[str, Any]:
    """Calendar and OHLC sanity of one market's bars (section 14, step 2)."""
    o, h, l, c = (frame[name] for name in ("open", "high", "low", "close"))
    flat = (h == l) & (l == c)
    gaps = frame.index.to_series().diff().dt.days
    log_change = np.log(c).diff().abs()
    return {"first": f"{frame.index[0]:%Y-%m-%d}", "last": f"{frame.index[-1]:%Y-%m-%d}", "rows": len(frame), "duplicates": int(frame.index.duplicated().sum()),
            "gaps over 5 days": int((gaps > 5).sum()), "high below open/close": int((h < np.maximum(o, c) - 1e-9).sum()),
            "low above open/close": int((l > np.minimum(o, c) + 1e-9).sum()), "bars with no range": int(flat.sum()),
            "last bar with no range": f"{frame.index[flat.to_numpy()][-1]:%Y-%m-%d}" if flat.any() else "-",
            "largest daily move": float(log_change.max()), "on": f"{log_change.idxmax():%Y-%m-%d}"}


CLOSE_ONLY_YEAR = 20  # a year with more bars than this whose high = low = close is close-only history


def usable_from(frame: pd.DataFrame) -> pd.Timestamp:
    """The first day after the last close-only year (early index history often has no high/low, which breaks ATR and
    ADX). Isolated bars without a range later on are kept: they are a data glitch on one day, not a missing field."""
    flat = (frame["high"] == frame["low"]) & (frame["low"] == frame["close"])
    per_year = flat.groupby(frame.index.year).sum()
    close_only = per_year[per_year > CLOSE_ONLY_YEAR]
    return pd.Timestamp(year=int(close_only.index.max()) + 1, month=1, day=1) if len(close_only) else frame.index[0]


def market_returns(config: Config, market: str, frame: pd.DataFrame, rates: pd.DataFrame) -> pd.Series:
    """What a rolled future on the index earns above cash per day: index return + (dividend yield - local rate) / 252."""
    from src.research import multi_asset as ma

    spec = config.markets[market]
    monthly = rates.copy()
    if spec["rate_area"] == "JPN" and "JPN" in monthly:
        first = monthly["JPN"].first_valid_index()
        monthly.loc[monthly.index < first, "JPN"] = config.execution.japan_rate_before_source
    rate = ma.daily_rates(monthly, frame.index)[spec["rate_area"]]
    return frame["close"].pct_change(fill_method=None) + (float(spec["dividend_yield"]) - rate) / 252.0


def load_rates() -> pd.DataFrame:
    """OECD 3-month rates by month (the H11 cache)."""
    from src.research import multi_asset as ma

    return _run(ma.load_raw(ma.load_markets()))["rates"]


# --- features, scores and the trend probability --------------------------------------------------------------------

def regime_scores(frame: pd.DataFrame, config: Config) -> pd.DataFrame:
    """Raw features, their walk-forward percentile scores, the trend probability and the stress flag, per bar."""
    settings = config.regime
    features = rf.build_features(frame, horizon=settings.horizon, adx_window=settings.adx, vr_lag=settings.vr_lag, short_vol=settings.short_vol,
                                 long_vol=settings.long_vol, breakout=settings.breakout, atr_window=config.trend.atr)
    percentile = lambda values: rf.calendar_percentile(values, calibration_days=settings.calibration_days, min_calibration=settings.min_calibration)  # noqa: E731
    for name, column in SCORE_FEATURES.items():
        features[f"{name}_score"] = percentile(features[column])
    weights = settings.weights
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError("regime weights must add up to 1")
    features["trend_probability"] = sum(weight * features[f"{name}_score"] for name, weight in weights.items())
    vol_percentile = percentile(features["short_vol"])
    previous_atr_share = (features["atr"] / frame["close"]).shift(1)
    features["stress"] = (vol_percentile > settings.stress_percentile) | (frame["close"].pct_change().abs() > settings.stress_gap_atr * previous_atr_share)
    features["vol_expanding"] = features["vol_ratio"] > 1.0
    features["direction_confirmed"] = (features["er_score"] > settings.direction_median) | (features["slope_score"] > settings.direction_median) | (features["breakout_score"] > settings.direction_median)
    return features


# --- the trend model -------------------------------------------------------------------------------------------------

def breakout_state(close: np.ndarray, high: np.ndarray, low: np.ndarray, entry: int, exit_window: int) -> np.ndarray:
    """+1 / 0 / -1: long after a close above the previous `entry`-day high until a close below the previous
    `exit_window`-day low; short symmetrically. Boundaries exclude the current bar."""
    upper, lower = rf.previous_high(high, entry), rf.previous_low(low, entry)
    exit_upper, exit_lower = rf.previous_high(high, exit_window), rf.previous_low(low, exit_window)
    state = np.zeros(len(close))
    current = 0.0
    for index in range(len(close)):
        if not (np.isfinite(upper[index]) and np.isfinite(lower[index])):
            continue
        if current > 0 and close[index] < exit_lower[index]:
            current = 0.0
        elif current < 0 and close[index] > exit_upper[index]:
            current = 0.0
        if close[index] > upper[index]:
            current = 1.0
        elif close[index] < lower[index]:
            current = -1.0
        state[index] = current
    return state


def trend_model(frame: pd.DataFrame, config: Config) -> pd.DataFrame:
    """The standalone trend model: 0.5 tanh(MA trend / 2) + 0.5 breakout state, clipped to [-1, 1]; 0 in warm-up."""
    settings = config.trend
    high, low, close = (frame[name].to_numpy(dtype=float) for name in ("high", "low", "close"))
    atr_values = atr(high, low, close, settings.atr)
    with np.errstate(divide="ignore", invalid="ignore"):
        ma_trend = (rolling_mean(close, settings.fast_mean) - rolling_mean(close, settings.slow_mean)) / atr_values
    ma_signal = np.tanh(ma_trend / settings.ma_scale)
    state = breakout_state(close, high, low, settings.breakout_entry, settings.breakout_exit)
    raw = np.clip(settings.ma_weight * ma_signal + settings.breakout_weight * state, -1.0, 1.0)
    return pd.DataFrame({"ma_trend": ma_trend, "ma_signal": ma_signal, "breakout_state": state, "trend_signal": np.nan_to_num(raw)}, index=frame.index)


# --- the regime state machine --------------------------------------------------------------------------------------

VARIANTS = ("blend", "hysteresis", "hard", "static", "probability", "filter", "trend_only", "range_only")


def classify(scores: pd.DataFrame, config: Config, *, variant: str = "blend", rapid: bool = True, filter_trend: pd.Series | None = None) -> pd.DataFrame:
    """Regime per bar and the two model weights.

    variant:
        blend: three states with hysteresis and the transition weight schedule (the primary strategy).
        hysteresis: the same states, with TRANSITION always at 0.25 / 0.25 (no schedule).
        hard: TREND when the trend probability >= 0.50, RANGE otherwise.
        static: no regime; 0.5 / 0.5 always.
        probability: no states; trend weight = trend probability, range weight = 1 - it (confidence scaling).
        filter: TREND where `filter_trend` is true, RANGE otherwise (the simple filters).
        trend_only / range_only: one model at full weight.
    rapid: the rapid-breakout exception (blend and hysteresis).
    """
    settings = config.regime
    index = scores.index
    p = scores["trend_probability"].to_numpy()
    n = len(index)
    state = np.array([TRANSITION] * n, dtype=object)
    trend_w, range_w = np.zeros(n), np.zeros(n)
    origin = np.array([""] * n, dtype=object)
    rapid_dir = np.zeros(n)
    candidate = np.array([TRANSITION] * n, dtype=object)
    days_in = np.zeros(n, dtype=int)

    if variant in ("static", "trend_only", "range_only", "probability", "hard", "filter"):
        state[:] = NONE
        if variant == "static":
            trend_w[:], range_w[:] = 0.5, 0.5
        elif variant == "trend_only":
            trend_w[:] = 1.0
        elif variant == "range_only":
            range_w[:] = 1.0
        elif variant == "probability":
            trend_w, range_w = np.nan_to_num(p), np.where(np.isfinite(p), 1.0 - np.nan_to_num(p), 0.0)
        else:
            flag = (p >= settings.exit_threshold) if variant == "hard" else np.asarray(filter_trend, dtype=bool)
            valid = np.isfinite(p) if variant == "hard" else np.ones(n, dtype=bool)
            state = np.where(valid, np.where(flag, TREND, RANGE), TRANSITION).astype(object)
            trend_w = np.where(state == TREND, 1.0, 0.0)
            range_w = np.where(state == RANGE, 1.0, 0.0)
        return pd.DataFrame({"state": state, "trend_weight": trend_w, "range_weight": range_w, "origin": origin, "rapid": rapid_dir,
                             "candidate": candidate, "days_in_state": days_in, "trend_probability": p}, index=index)

    component = scores[[f"{name}_score" for name in settings.weights]].to_numpy()
    vol_expanding = scores["vol_expanding"].to_numpy(dtype=bool)
    confirmed = scores["direction_confirmed"].to_numpy(dtype=bool)
    upper, lower = scores["upper_distance"].to_numpy(), scores["lower_distance"].to_numpy()
    direction_strong = (scores["er_score"].to_numpy() > settings.direction_median) | (scores["slope_score"].to_numpy() > settings.direction_median)

    current, came_from, since = TRANSITION, "", 0
    trend_run = range_run = exit_run = 0
    for t in range(n):
        if not np.isfinite(p[t]):
            state[t] = TRANSITION
            continue
        high_scores, low_scores = int(np.sum(component[t] >= 0.65)), int(np.sum(component[t] <= 0.35))
        conflicting = high_scores >= 2 and low_scores >= 2
        if p[t] >= settings.trend_entry and not conflicting and not (vol_expanding[t] and not confirmed[t]):
            cand = TREND
        elif p[t] <= settings.range_entry and not conflicting:
            cand = RANGE
        else:
            cand = TRANSITION
        candidate[t] = cand
        trend_run = trend_run + 1 if cand == TREND else 0
        range_run = range_run + 1 if cand == RANGE else 0
        needed_trend = settings.confirm_fast if (vol_expanding[t] and confirmed[t]) else settings.confirm

        previous = current
        if current == TREND:
            exit_run = exit_run + 1 if p[t] < settings.exit_threshold else 0
            if exit_run >= settings.exit_confirm:
                current, came_from, exit_run = TRANSITION, TREND, 0
        elif current == RANGE:
            beyond = np.nanmax([upper[t], lower[t]])
            if rapid and np.isfinite(beyond) and beyond >= settings.rapid_breakout_atr and vol_expanding[t] and direction_strong[t]:
                current, came_from, exit_run = TRANSITION, RANGE, 0
                rapid_dir[t] = 1.0 if upper[t] >= lower[t] else -1.0
            else:
                exit_run = exit_run + 1 if p[t] > settings.exit_threshold else 0
                if exit_run >= settings.exit_confirm:
                    current, came_from, exit_run = TRANSITION, RANGE, 0
        else:  # TRANSITION: commit after enough consecutive candidates
            if trend_run >= needed_trend:
                current, exit_run = TREND, 0
            elif range_run >= settings.confirm:
                current, exit_run = RANGE, 0
        since = since + 1 if current == previous else 1
        state[t], origin[t], days_in[t] = current, came_from if current == TRANSITION else "", since

        if current == TREND:
            trend_w[t], range_w[t] = 1.0, 0.0
        elif current == RANGE:
            trend_w[t], range_w[t] = 0.0, 1.0
        elif variant == "hysteresis" or came_from == "":
            trend_w[t], range_w[t] = 0.25, 0.25
        elif came_from == RANGE:  # day 1: 0.25 / 0.25; from day 2, while trend evidence persists: 0.50 trend
            trend_w[t], range_w[t] = (0.5, 0.0) if since >= 2 and p[t] >= settings.exit_threshold else (0.25, 0.25)
        else:  # from TREND: day 1: 0.50 trend; from day 2, while range evidence persists: 0.25 / 0.25
            trend_w[t], range_w[t] = (0.25, 0.25) if since >= 2 and p[t] <= settings.exit_threshold else (0.5, 0.0)
    return pd.DataFrame({"state": state, "trend_weight": trend_w, "range_weight": range_w, "origin": origin, "rapid": rapid_dir,
                         "candidate": candidate, "days_in_state": days_in, "trend_probability": p}, index=index)


# --- the range model -----------------------------------------------------------------------------------------------

def range_inputs(frame: pd.DataFrame, config: Config) -> pd.DataFrame:
    """Anchor, residual z-score, previous range boundaries and ATR for the range model."""
    settings = config.range
    high, low, close = (frame[name].to_numpy(dtype=float) for name in ("high", "low", "close"))
    anchor = ema(close, settings.anchor_span)
    residual = close - anchor
    residual_vol = pd.Series(residual).rolling(settings.residual_vol).std().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        z = residual / residual_vol
    z[: settings.anchor_span] = np.nan  # the EWMA is seeded with the first close: give it time
    return pd.DataFrame({"anchor": anchor, "residual": residual, "z": z, "range_high": rf.previous_high(high, settings.boundary),
                         "range_low": rf.previous_low(low, settings.boundary), "atr": atr(high, low, close, config.trend.atr)}, index=frame.index)


def range_model(frame: pd.DataFrame, inputs: pd.DataFrame, trend: pd.DataFrame, scores: pd.DataFrame, config: Config, *,
                regime: pd.DataFrame | None = None, version: str = "A") -> tuple[pd.DataFrame, pd.DataFrame]:
    """The range model's signal per bar (0 when flat), with its trade log.

    version: "A" symmetric, "B" short signal capped at 0.5, "C" no shorts (a diagnostic only).
    regime: the overlay's output; None runs the model standalone (entries need no regime).
    """
    settings, stress_settings = config.range, config.regime
    close = frame["close"].to_numpy(dtype=float)
    z, high_b, low_b, atr_v = (inputs[name].to_numpy() for name in ("z", "range_high", "range_low", "atr"))
    trend_signal = trend["trend_signal"].to_numpy()
    breakout = trend["breakout_state"].to_numpy()
    vol_ratio = scores["vol_ratio"].to_numpy()
    stress = scores["stress"].to_numpy(dtype=bool)
    n = len(close)
    state = regime["state"].to_numpy() if regime is not None else None
    range_w = regime["range_weight"].to_numpy() if regime is not None else None
    rapid = regime["rapid"].to_numpy() if regime is not None else np.zeros(n)
    signal = np.zeros(n)
    side, held, block = 0, 0, 0
    trades: list[dict[str, Any]] = []
    for t in range(n):
        block = stress_settings.stress_range_block if stress[t] else max(0, block - 1)
        if not np.isfinite(z[t]) or not np.isfinite(atr_v[t]) or not np.isfinite(high_b[t]):
            continue
        veto = np.isfinite(vol_ratio[t]) and vol_ratio[t] > settings.vol_veto_ratio
        above = close[t] > high_b[t] + settings.boundary_atr * atr_v[t]
        below = close[t] < low_b[t] - settings.boundary_atr * atr_v[t]
        exited = False
        if side != 0:
            held += 1
            reason = None
            if (side > 0 and z[t] >= 0.0) or (side < 0 and z[t] <= 0.0):
                reason = "z crossed zero"
            elif side < 0 and above or side > 0 and below:
                reason = "boundary stop"
            elif rapid[t] == -side or breakout[t] == -side and (side < 0 and close[t] > high_b[t] or side > 0 and close[t] < low_b[t]):
                reason = "opposite breakout"
            elif veto:
                reason = "volatility veto"
            elif state is not None and state[t] != RANGE and range_w[t] < settings.regime_exit_weight:
                reason = "regime left RANGE"
            elif held >= settings.max_hold:
                reason = "max hold"
            if reason:
                trades[-1].update({"exit": frame.index[t], "reason": reason, "days": held})
                side, held, exited = 0, 0, True
            else:
                size = float(np.clip(-z[t] / settings.signal_scale, -1.0, 1.0))
                size = size if np.sign(size) == side else 0.0
                if side < 0 and version == "B":
                    size = max(size, -settings.short_cap_b)
                if block > 0:  # in stress an open range trade may only shrink
                    size = side * min(abs(size), abs(signal[t - 1]))
                signal[t] = size
        if side == 0 and not exited and block == 0 and not veto:
            eligible = state is None or state[t] == RANGE or range_w[t] >= settings.regime_entry_weight
            if eligible and z[t] <= -settings.entry_z and not below and trend_signal[t] > -settings.strong_trend:
                side = 1
            elif eligible and version != "C" and z[t] >= settings.entry_z and not above and trend_signal[t] < settings.strong_trend:
                side = -1
            if side != 0:
                size = float(np.clip(-z[t] / settings.signal_scale, -1.0, 1.0))
                signal[t] = max(size, -settings.short_cap_b) if side < 0 and version == "B" else size
                held = 0
                trades.append({"side": side, "entry": frame.index[t], "exit": None, "reason": None, "days": None})
    return pd.DataFrame({"range_signal": signal}, index=frame.index).assign(range_side=np.sign(signal)), pd.DataFrame(trades)


# --- combining the models ----------------------------------------------------------------------------------------

def combine(trend: pd.DataFrame, range_signal: pd.Series, regime: pd.DataFrame, scores: pd.DataFrame, config: Config, *,
            overlay_rules: bool = True) -> pd.DataFrame:
    """The final target signal in [-1, 1].

    The weighted signals are summed; in TRANSITION the gross model exposure is capped at `transition_cap` (both
    scaled down); in a committed regime only that regime's model counts. With `overlay_rules` the stress rules (half
    the signal, at most a 0.5 step per bar) and the dead zone apply, as for every combination; the standalone models
    and the long references run without them.
    """
    settings = config.regime
    ts = trend["trend_signal"].to_numpy()
    rs = range_signal.to_numpy()
    tw, rw = regime["trend_weight"].to_numpy(), regime["range_weight"].to_numpy()
    state = regime["state"].to_numpy()
    trend_part, range_part = tw * ts, rw * rs
    in_transition = state == TRANSITION
    gross = np.abs(trend_part) + np.abs(range_part)
    scale = np.where(in_transition & (gross > settings.transition_cap), settings.transition_cap / np.where(gross > 0, gross, 1.0), 1.0)
    trend_part, range_part = trend_part * scale, range_part * scale
    trend_part = np.where(state == RANGE, 0.0, trend_part)
    range_part = np.where(state == TREND, 0.0, range_part)
    combined = np.clip(trend_part + range_part, -1.0, 1.0)
    if overlay_rules:
        stress = scores["stress"].to_numpy(dtype=bool)
        combined = np.where(stress, combined * settings.stress_signal_scale, combined)
        for t in range(1, len(combined)):
            if stress[t]:
                step = combined[t] - combined[t - 1]
                if abs(step) > settings.stress_max_step:
                    combined[t] = combined[t - 1] + math.copysign(settings.stress_max_step, step)
        combined = np.where(np.abs(combined) < settings.dead_zone, 0.0, combined)
    return pd.DataFrame({"trend_part": trend_part, "range_part": range_part, "target_signal": combined}, index=trend.index)


# --- execution, P&L, trades ----------------------------------------------------------------------------------------

@dataclass(slots=True)
class Run:
    """One strategy on one market: daily series and its trades (from the executed position)."""

    name: str
    signal: pd.Series
    decided: pd.Series
    earning: pd.Series
    gross: pd.Series
    net: pd.Series
    costs: pd.Series
    traded: pd.Series
    extra: dict[str, Any] = field(default_factory=dict)


def vol_scale(returns: pd.Series, config: Config) -> pd.Series:
    """target_vol / annualised EWMA volatility, capped: the size of a unit signal."""
    sigma = pd.Series(ewma_volatility(returns.to_numpy(), span=config.execution.vol_span), index=returns.index) * math.sqrt(252.0)
    return (config.execution.target_vol / sigma).clip(upper=config.execution.max_position)


def backtest(name: str, signal: pd.Series, returns: pd.Series, config: Config, *, cost_bps: float, scale: pd.Series | None = None,
             cost_multiplier: float = 1.0, unit_scale: bool = False) -> Run:
    """Target = signal x vol scale (or x 1 when `unit_scale`), the band, then decided at t, traded at t+1, earning from t+2."""
    execution = config.execution
    sized = signal.fillna(0.0) * (1.0 if unit_scale else (scale if scale is not None else vol_scale(returns, config)).fillna(0.0))
    sized = sized.clip(-execution.max_position, execution.max_position)
    decided = np.zeros(len(sized))
    current = 0.0
    for t, value in enumerate(sized.to_numpy()):
        if (value == 0.0 and current != 0.0) or abs(value - current) > execution.rebalance_band:
            current = value
        decided[t] = current
    decided = pd.Series(decided, index=signal.index)
    earning = decided.shift(execution.trade_lag).fillna(0.0)  # traded at the close of t+1, so it earns from t+2
    gross = earning * returns.fillna(0.0)
    traded = decided.diff().abs().fillna(decided.abs()).shift(execution.trade_lag - 1).fillna(0.0)
    per_side = cost_bps / 10_000.0 * cost_multiplier
    costs = traded * per_side + earning.abs() * execution.rolls_per_year * execution.roll_bps / 10_000.0 / 252.0 * cost_multiplier
    return Run(name=name, signal=signal, decided=decided, earning=earning, gross=gross, net=gross - costs, costs=costs, traded=traded)


def trade_records(run: Run) -> pd.DataFrame:
    """Trades of the executed position: runs of days holding the same sign, with their P&L."""
    side = np.sign(run.earning.to_numpy())
    net = run.net.to_numpy()
    rows, start = [], None
    for t in range(len(side) + 1):
        current = side[t] if t < len(side) else 0.0
        if start is not None and (t == len(side) or current != side[start]):
            rows.append({"entry": run.earning.index[start], "exit": run.earning.index[t - 1], "side": int(side[start]), "days": t - start,
                         "pnl": float(net[start:t].sum())})
            start = None
        if start is None and t < len(side) and current != 0.0:
            start = t
    return pd.DataFrame(rows, columns=["entry", "exit", "side", "days", "pnl"])


def metrics(run: Run, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> dict[str, float]:
    """The section-10 table for one run over [start, end]."""
    window = slice(start, end)
    net, gross, earning = run.net.loc[window], run.gross.loc[window], run.earning.loc[window]
    if len(net) < 20:
        return {}
    annual = 252.0
    vol = net.std() * math.sqrt(annual)
    downside = math.sqrt(float(np.mean(np.minimum(net.to_numpy(), 0.0) ** 2))) * math.sqrt(annual)
    wealth = (1.0 + net).cumprod()
    max_dd = float((wealth / wealth.cummax() - 1.0).min())
    ann_return = float(net.mean() * annual)
    trades = trade_records(Run(run.name, run.signal.loc[window], run.decided.loc[window], earning, gross, net, run.costs.loc[window], run.traded.loc[window]))
    wins, losses = trades[trades["pnl"] > 0]["pnl"], trades[trades["pnl"] <= 0]["pnl"]
    years = len(net) / annual
    return {"annual return": ann_return, "volatility": float(vol), "Sharpe": float(net.mean() / net.std() * math.sqrt(annual)) if net.std() > 0 else float("nan"),
            "Sortino": ann_return / downside if downside > 0 else float("nan"), "max drawdown": max_dd, "Calmar": ann_return / abs(max_dd) if max_dd < 0 else float("nan"),
            "turnover": float(run.traded.loc[window].sum() / years), "trades": float(len(trades)), "avg holding days": float(trades["days"].mean()) if len(trades) else float("nan"),
            "hit rate": float(len(wins) / len(trades)) if len(trades) else float("nan"), "avg win": float(wins.mean()) if len(wins) else float("nan"),
            "avg loss": float(losses.mean()) if len(losses) else float("nan"),
            "profit factor": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else float("nan"),
            "exposure": float(earning.abs().mean()), "time invested": float((earning != 0).mean()),
            "long contribution": float(net[earning > 0].sum() / years), "short contribution": float(net[earning < 0].sum() / years),
            "costs per year": float(run.costs.loc[window].sum() / years),
            "gross Sharpe": float(gross.mean() / gross.std() * math.sqrt(annual)) if gross.std() > 0 else float("nan")}


# --- one market, every predefined strategy -------------------------------------------------------------------------

@dataclass(slots=True)
class MarketStudy:
    """Everything computed for one market under one configuration."""

    market: str
    frame: pd.DataFrame
    returns: pd.Series
    scores: pd.DataFrame
    trend: pd.DataFrame
    range_inputs: pd.DataFrame
    regimes: dict[str, pd.DataFrame]
    range_signals: dict[str, pd.Series]
    targets: dict[str, pd.DataFrame]
    runs: dict[str, Run]


def simple_filters(frame: pd.DataFrame, scores: pd.DataFrame, config: Config) -> dict[str, pd.Series]:
    """The section-12M filters as TREND (true) / RANGE (false) per bar; each is fixed in the config."""
    close = frame["close"]
    filters = config.filters
    ma_long = close.rolling(filters["ma_long"]).mean()
    fast, slow = (close.rolling(window).mean() for window in filters["fast_slow"])
    recent_breakout = (scores["breakout_beyond"] > 0).rolling(20, min_periods=1).max().astype(bool)
    out = {"M1 above 200-day MA": close > ma_long,
           "M2 |40-day return| > 10%": (close / close.shift(filters["return_window"]) - 1.0).abs() > filters["return_threshold"],
           "M4 ER above median": scores["er_score"] > 0.5,
           "M5 20-day MA above 100-day MA": fast > slow,
           "M6 vol expansion with breakout": scores["vol_expanding"] & recent_breakout}
    for level in filters["adx_levels"]:
        out[f"M3 ADX > {level}"] = scores["adx"] > level
    return {name: series.fillna(False) for name, series in out.items()}


def ablation_scores(scores: pd.DataFrame, config: Config, *, drop: str | None = None, only: str | None = None) -> pd.DataFrame:
    """The trend probability without one feature (weights renormalised) or from one feature alone."""
    weights = dict(config.regime.weights)
    if drop:
        weights.pop(drop)
    if only:
        weights = {only: 1.0}
    total = sum(weights.values())
    out = scores.copy()
    out["trend_probability"] = sum(weight / total * scores[f"{name}_score"] for name, weight in weights.items())
    return out


def run_market(config: Config, market: str, frame: pd.DataFrame, rates: pd.DataFrame, *, cost_multiplier: float = 1.0, full: bool = True) -> MarketStudy:
    """Every predefined strategy on one market. `full=False` runs only the primary and the main baselines (cross-market)."""
    cost_bps = float(config.markets[market]["cost_bps"])
    returns = market_returns(config, market, frame, rates)
    scores = regime_scores(frame, config)
    trend = trend_model(frame, config)
    inputs = range_inputs(frame, config)
    scale = vol_scale(returns, config)
    regimes: dict[str, pd.DataFrame] = {}
    range_signals: dict[str, pd.Series] = {}
    targets: dict[str, pd.DataFrame] = {}
    runs: dict[str, Run] = {}

    def add(name: str, variant: str, *, version: str = "A", rapid: bool = True, filter_trend: pd.Series | None = None, score_frame: pd.DataFrame | None = None,
            standalone_range: bool = False) -> None:
        used = score_frame if score_frame is not None else scores
        regime = classify(used, config, variant=variant, rapid=rapid, filter_trend=filter_trend)
        overlay = None if (standalone_range or variant in ("static", "range_only", "trend_only", "probability")) else regime
        ranged, _trades = range_model(frame, inputs, trend, used, config, regime=overlay, version=version)
        target = combine(trend, ranged["range_signal"], regime, used, config, overlay_rules=variant not in ("trend_only", "range_only"))
        regimes[name], range_signals[name], targets[name] = regime, ranged["range_signal"], target
        runs[name] = backtest(name, target["target_signal"], returns, config, cost_bps=cost_bps, scale=scale, cost_multiplier=cost_multiplier)

    add("1 trend alone", "trend_only")
    for version in ("A", "B"):
        add(f"2{version} range alone", "range_only", version=version, standalone_range=True)
        add(f"3{version} hard switch", "hard", version=version)
        add(f"4{version} three-state, hysteresis", "hysteresis", version=version)
        add(f"5{version} three-state, transition blending", "blend", version=version)
        add(f"6{version} static 50/50", "static", version=version)
    filters = simple_filters(frame, scores, config)
    add("7A 200-day MA regime filter", "filter", filter_trend=filters["M1 above 200-day MA"])
    long_signal = pd.Series(1.0, index=frame.index)
    runs["8 buy and hold (1x)"] = backtest("8 buy and hold (1x)", long_signal, returns, config, cost_bps=cost_bps, cost_multiplier=cost_multiplier, unit_scale=True)
    runs["8v volatility-managed long"] = backtest("8v volatility-managed long", long_signal, returns, config, cost_bps=cost_bps, scale=scale, cost_multiplier=cost_multiplier)
    primary, static = targets["5A three-state, transition blending"]["target_signal"], targets["6A static 50/50"]["target_signal"]
    matched = primary.abs() * np.sign(static)
    runs["9A exposure-matched static"] = backtest("9A exposure-matched static", matched, returns, config, cost_bps=cost_bps, scale=scale, cost_multiplier=cost_multiplier)
    if full:
        ma_filtered = (frame["close"] > frame["close"].rolling(config.filters["ma_long"]).mean()).astype(float)
        runs["R MA-filtered long"] = backtest("R MA-filtered long", ma_filtered, returns, config, cost_bps=cost_bps, scale=scale, cost_multiplier=cost_multiplier)
        add("5C three-state, no range shorts (diagnostic)", "blend", version="C")
        add("5A without rapid-breakout exception", "blend", rapid=False)
        add("P probability-weighted (confidence scaling)", "probability")
        for name, flag in filters.items():
            if not name.startswith("M1"):
                add(f"F {name}", "filter", filter_trend=flag)
        for feature in config.regime.weights:
            add(f"X without {feature}", "blend", score_frame=ablation_scores(scores, config, drop=feature))
            add(f"S only {feature}", "blend", score_frame=ablation_scores(scores, config, only=feature))
    return MarketStudy(market, frame, returns, scores, trend, inputs, regimes, range_signals, targets, runs)


PRIMARY = "5A three-state, transition blending"


def window(series: pd.Series | pd.DataFrame, config: Config) -> Any:
    """The evaluation window."""
    return series.loc[pd.Timestamp(config.data["evaluation_start"]):pd.Timestamp(config.data["evaluation_end"])]
