"""The H14 regime overlay as quantlab components: a trend model, a range model, a feature-blend regime machine and
the overlay that weights them (the owner's specification; research/prereg/H14.txt in the host repository).

Ported from the host's src/research/regime_switch.py (docs/quantlab_migration.md): the functions are the same code,
with the engine's execution, sizing and costs removed (quantlab.engine does those for every strategy). Settings come
from the same TOML sections ([trend], [range], [regime]); `RegimeOverlayStrategy` turns them into engine targets.

Variants (`classify(..., variant=)`): blend (primary), hysteresis, hard, static, probability, filter, trend_only,
range_only. Range versions: A symmetric, B short capped at 0.5, C no shorts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pandas as pd

from quantlab.data.panel import Panel
from quantlab.engine.cache import cached
from quantlab.engine.components import Targets
from quantlab.features import regime as rf
from quantlab.features.indicators import atr, ema, rolling_mean

RANGE, TRANSITION, TREND, NONE = "RANGE", "TRANSITION", "TREND", "NONE"  # NONE: a variant without regimes
STATE_CODE = {RANGE: -1, TRANSITION: 0, TREND: 1, NONE: 0}
SCORE_FEATURES = {"er": "er", "slope": "abs_slope_t", "adx": "adx", "vr": "vr", "breakout": "breakout_beyond"}


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
class OverlayConfig:
    """The three settings sections the overlay reads ([trend], [range], [regime] of a TOML file)."""

    trend: TrendSettings = field(default_factory=TrendSettings)
    range: RangeSettings = field(default_factory=RangeSettings)
    regime: RegimeSettings = field(default_factory=RegimeSettings)
    filters: dict[str, Any] = field(default_factory=lambda: {"ma_long": 200, "return_window": 40, "return_threshold": 0.10, "adx_levels": [20, 25], "fast_slow": [20, 100]})

    @classmethod
    def from_toml(cls, path: str) -> "OverlayConfig":
        """The [trend], [range], [regime] and [filters] sections of a study's TOML file, parsed strictly."""
        import tomllib
        from pathlib import Path

        from quantlab.config import parse

        raw = tomllib.loads(Path(path).read_text())
        return cls(trend=parse(raw.get("trend", {}), TrendSettings), range=parse(raw.get("range", {}), RangeSettings),
                   regime=parse(raw.get("regime", {}), RegimeSettings), filters=dict(raw.get("filters", cls().filters)))

    def with_changes(self, **changes: Any) -> "OverlayConfig":
        """A copy with section settings changed, e.g. regime={"horizon": 30}."""
        return replace(self, **{name: replace(getattr(self, name), **values) for name, values in changes.items()})


@cached
def regime_scores(frame: pd.DataFrame, config: OverlayConfig) -> pd.DataFrame:
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


def trend_model(frame: pd.DataFrame, config: OverlayConfig) -> pd.DataFrame:
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


VARIANTS = ("blend", "hysteresis", "hard", "static", "probability", "filter", "trend_only", "range_only")


def classify(scores: pd.DataFrame, config: OverlayConfig, *, variant: str = "blend", rapid: bool = True, filter_trend: pd.Series | None = None) -> pd.DataFrame:
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


def range_inputs(frame: pd.DataFrame, config: OverlayConfig) -> pd.DataFrame:
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


def range_model(frame: pd.DataFrame, inputs: pd.DataFrame, trend: pd.DataFrame, scores: pd.DataFrame, config: OverlayConfig, *,
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


def combine(trend: pd.DataFrame, range_signal: pd.Series, regime: pd.DataFrame, scores: pd.DataFrame, config: OverlayConfig, *,
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


def simple_filters(frame: pd.DataFrame, scores: pd.DataFrame, config: OverlayConfig) -> dict[str, pd.Series]:
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


def ablation_scores(scores: pd.DataFrame, config: OverlayConfig, *, drop: str | None = None, only: str | None = None) -> pd.DataFrame:
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




def ablation_scores(scores: pd.DataFrame, config: OverlayConfig, *, drop: str | None = None, only: str | None = None) -> pd.DataFrame:
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


@dataclass
class RegimeOverlayStrategy:
    """One H14 variant as an engine strategy: per instrument, scores -> regime -> range model -> combined target.

    Attributes:
        config: Settings.
        variant: The regime variant (see `classify`).
        version: Range-model version A, B or C.
        rapid: The rapid-breakout exception.
        standalone_range: Run the range model without the overlay's entry/exit conditions (the "range alone" baseline).
        overlay_rules: Stress scaling and the dead zone (on for every combination; off for the standalone models).
        filter_name: For variant="filter", which simple filter (a key of `simple_filters`).
        score_transform: Optional function of the score frame (ablations, a notebook's own trend probability).
    """

    config: OverlayConfig = field(default_factory=OverlayConfig)
    variant: str = "blend"
    version: str = "A"
    rapid: bool = True
    standalone_range: bool = False
    overlay_rules: bool | None = None
    filter_name: str | None = None
    score_transform: Any = None

    def instrument(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Target, parts and diagnostics for one instrument's bars."""
        config = self.config
        scores = regime_scores(bars, config)
        if self.score_transform is not None:
            scores = self.score_transform(scores)
        trend = trend_model(bars, config)
        inputs = range_inputs(bars, config)
        flag = simple_filters(bars, scores, config)[self.filter_name] if self.variant == "filter" else None
        regime = classify(scores, config, variant=self.variant, rapid=self.rapid, filter_trend=flag)
        independent = self.standalone_range or self.variant in ("static", "range_only", "trend_only", "probability")
        ranged, _trades = range_model(bars, inputs, trend, scores, config, regime=None if independent else regime, version=self.version)
        rules = self.overlay_rules if self.overlay_rules is not None else self.variant not in ("trend_only", "range_only")
        target = combine(trend, ranged["range_signal"], regime, scores, config, overlay_rules=rules)
        return pd.DataFrame({"target": target["target_signal"], "trend_part": target["trend_part"], "range_part": target["range_part"],
                             "state": regime["state"].map(STATE_CODE).astype(float), "trend_weight": regime["trend_weight"],
                             "range_weight": regime["range_weight"], "trend_probability": scores["trend_probability"],
                             "stress": scores["stress"].astype(float), "trend_signal": trend["trend_signal"], "range_signal": ranged["range_signal"]}, index=bars.index)

    def __call__(self, panel: Panel) -> Targets:
        from quantlab.engine.components import per_instrument

        columns = per_instrument(panel, lambda _instrument, bars: self.instrument(bars))
        parts = {"trend": columns["trend_part"], "range": columns["range_part"]}
        diagnostics = {name: columns[name] for name in ("state", "trend_weight", "range_weight", "trend_probability", "stress", "trend_signal", "range_signal")}
        return Targets(columns["target"], parts, diagnostics)
