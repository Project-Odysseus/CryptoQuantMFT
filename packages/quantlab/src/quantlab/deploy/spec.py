"""One strategy object from research to paper to live (plan 7.1).

A strategy is a versioned spec file: the signal (by name and parameters), how it is sized and traded, its universe,
what research expects of it, and what it can hold. Research backtests it, the promotion gates judge it, and paper or
live books run the *same* spec through the same signal code (the host's `lab` strategy builds from it). Promotion
moves a spec id and version between stages; nothing is rewritten. Any change to the file is a new version, and its
hash no longer matches the one that was promoted, which the gates and the host's portfolio check catch.

    spec = load_spec("research/strategies/smooth_trend.toml")
    spec.forecast(bars)                 # one instrument's forecast, as paper and live compute it
    result = backtest_spec(spec)        # the research engine on the spec's universe

The file:

    [strategy]  id, version, title, hypothesis, description
    [signal]    name = a quantlab.signals.library name or "module:function"; params = {...}
    [sizing]    target_vol, span, cap
    [engine]    band, band_exits_to_zero, max_position, cost_bps (omit: the master's costs), rolls_per_year, roll_bps
    [universe]  instruments = [...]   (equal weights)
    [expectations]  sharpe, shrunk_sharpe, volatility, max_drawdown, turnover, costs_per_year, source
    [capacity]  adv_usd = {instrument: average daily traded value}, max_participation, impact_coefficient
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from quantlab.config import config_hash, parse
from quantlab.signals import library as lib


@dataclass(frozen=True)
class SignalSpec:
    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SizingSpec:
    target_vol: float = 0.10
    span: int = 20
    cap: float | None = None


@dataclass(frozen=True)
class EngineSpec:
    band: float = 0.02
    band_exits_to_zero: bool = True
    max_position: float = 2.0
    cost_bps: float | None = None
    rolls_per_year: float = 4.0
    roll_bps: float = 1.0


@dataclass(frozen=True)
class UniverseSpec:
    instruments: tuple[str, ...]


@dataclass(frozen=True)
class Expectations:
    """What research says to expect live; the monitor (quantlab.deploy.monitor) holds paper and live to it."""

    sharpe: float | None = None
    shrunk_sharpe: float | None = None
    volatility: float | None = None
    max_drawdown: float | None = None
    turnover: float | None = None
    costs_per_year: float | None = None
    source: str = ""


@dataclass(frozen=True)
class CapacitySpec:
    adv_usd: dict[str, float] = field(default_factory=dict)
    max_participation: float = 0.01
    impact_coefficient: float = 0.01  # about Y x daily volatility (Y ~ 1): 1% participation costs ~10 bps on equity indices


@dataclass(frozen=True)
class StrategyHeader:
    id: str
    version: int
    title: str
    hypothesis: str = ""
    description: str = ""


@dataclass(frozen=True)
class StrategySpec:
    """A parsed spec file (see the module docstring)."""

    strategy: StrategyHeader
    signal: SignalSpec
    universe: UniverseSpec
    sizing: SizingSpec = field(default_factory=SizingSpec)
    engine: EngineSpec = field(default_factory=EngineSpec)
    expectations: Expectations = field(default_factory=Expectations)
    capacity: CapacitySpec = field(default_factory=CapacitySpec)

    @property
    def id(self) -> str:
        return self.strategy.id

    @property
    def version(self) -> int:
        return self.strategy.version

    @property
    def hash(self) -> str:
        """Fingerprint of everything in the spec: promoted versions are recorded with it."""
        return config_hash(self)

    @property
    def key(self) -> str:
        return f"{self.id}@v{self.version}"

    def signal_fn(self) -> Callable[[pd.DataFrame], pd.Series]:
        """The per-instrument signal (bars -> forecast), built from its name and parameters."""
        params = _tuples(dict(self.signal.params))
        if ":" in self.signal.name:
            module, name = self.signal.name.split(":", 1)
            return getattr(importlib.import_module(module), name)(**params)
        if self.signal.name not in lib.CATALOG:
            raise KeyError(f"unknown signal {self.signal.name!r}: a quantlab.signals.library name ({sorted(lib.CATALOG)}) or 'module:function'")
        return lib.CATALOG[self.signal.name](**params)

    def forecast(self, bars: pd.DataFrame) -> pd.Series:
        """One instrument's forecast at every bar (known at that bar's close), clipped to the forecast cap."""
        return self.signal_fn()(bars).astype(float).clip(-2.0, 2.0)


def _tuples(value: Any) -> Any:
    """TOML has lists only; signal parameters such as speeds are tuples of tuples."""
    if isinstance(value, list):
        return tuple(_tuples(item) for item in value)
    if isinstance(value, dict):
        return {key: _tuples(item) for key, item in value.items()}
    return value


def load_spec(path: str | Path) -> StrategySpec:
    """Parse a spec file strictly (unknown keys and wrong types are errors)."""
    import tomllib

    return parse(tomllib.loads(Path(path).read_text(encoding="utf-8")), StrategySpec)


def find_spec(name_or_path: str, *, root: Path | None = None) -> Path:
    """A spec's file from its id (research/strategies/<id>.toml under the project root) or a path."""
    path = Path(name_or_path)
    if path.suffix == ".toml" and path.exists():
        return path
    from quantlab.lab import project_root

    base = root or project_root() or Path.cwd()
    candidate = base / "research" / "strategies" / f"{name_or_path}.toml"
    if not candidate.exists():
        raise FileNotFoundError(f"no strategy spec {name_or_path!r} (looked for {candidate})")
    return candidate


def spec_lab(spec: StrategySpec, *, start: str | None = None, end: str | None = None, bars: Mapping[str, pd.DataFrame] | None = None, **lab_options: Any) -> Any:
    """A Lab on the spec's universe, sized and traded as the spec says."""
    from quantlab.engine.backtest import EngineConfig, Sizer
    from quantlab.lab import Lab

    engine = EngineConfig(band=spec.engine.band, band_exits_to_zero=spec.engine.band_exits_to_zero, max_position=spec.engine.max_position,
                          rolls_per_year=spec.engine.rolls_per_year, roll_bps=spec.engine.roll_bps)
    sizer = Sizer(spec.sizing.target_vol, span=spec.sizing.span, cap=spec.sizing.cap)
    options = {"show": False, **lab_options}
    if spec.engine.cost_bps is not None:
        options["cost_bps"] = spec.engine.cost_bps
    lab = Lab(universe=list(spec.universe.instruments), start=start, end=end, bars=bars, engine=engine, sizer=sizer, **options)
    if spec.engine.cost_bps is not None and bars is None:
        lab.costs = spec.engine.cost_bps
    return lab


def backtest_spec(spec: StrategySpec, *, lab: Any = None, **lab_options: Any) -> Any:
    """The research engine's result for the spec (its Lab is built unless given)."""
    lab = lab or spec_lab(spec, **lab_options)
    idea = lab.signal(spec.signal_fn(), name=spec.key, normalize=False, check=False)
    return lab.backtest(idea)
