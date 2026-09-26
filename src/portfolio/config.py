"""Portfolio configuration: one TOML file describes instruments, sleeves, allocation and risk limits.

`load_portfolio_config(path)` reads the file (with the standard library's
`tomllib`), checks every rule and raises one `PortfolioConfigError` listing
*all* problems, each naming the key to fix. It checks strategies, parameters
and sizers by building them. An unknown key is an error, so a typo can't
silently fall back to a default. `describe(config)` prints the resolved
setup (`main.py --portfolio-check`).

See config/portfolio.example.toml for a commented example and
docs/portfolio_plan.md section 5 for every field.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from src.portfolio.allocation import ALLOCATION_METHODS
from src.portfolio.risk import PortfolioRiskConfig
from src.portfolio.sleeves import STOP_KEYS, SleeveSpec
from src.runtime.config import BAR_INTERVALS

INTERVALS = tuple(BAR_INTERVALS)  # the runtime's bar lengths, so a config never asks for one it can't build
_SLUG = re.compile(r"^[a-z0-9_]+$")
DEFAULT_FEE_PCT = {"perp": 0.05, "spot": 0.40}  # Kraken taker, entry tier


class PortfolioConfigError(ValueError):
    """A portfolio config has problems; the message lists all of them."""


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """One tradable instrument (an `[instruments."venue:symbol"]` table).

    Attributes:
        id: "<venue>:<symbol>".
        kind: "perp" or "spot".
        allow_short: Defaults to True for perps and False for spot.
        max_leverage: Largest position as a multiple of this venue's equity (perps).
        fee_pct: Taker fee per side in % (research costs); defaults by kind.
        slippage_bps: Per side (research costs).
        min_order_size, lot_step: Order limits in units; 0 means unknown (the
            runtime then reads them from the exchange).
    """

    id: str
    kind: str = "perp"
    allow_short: bool | None = None
    max_leverage: float = 1.0
    fee_pct: float | None = None
    slippage_bps: float = 5.0
    min_order_size: float = 0.0
    lot_step: float = 0.0

    @property
    def venue(self) -> str:
        """Venue name, e.g. kraken_futures."""
        return self.id.split(":", 1)[0]

    @property
    def symbol(self) -> str:
        """Symbol at the venue, e.g. BTC/USD."""
        return self.id.split(":", 1)[1]

    @property
    def can_short(self) -> bool:
        """Whether a net short position is allowed."""
        return self.allow_short if self.allow_short is not None else self.kind == "perp"

    @property
    def taker_fee_pct(self) -> float:
        """The fee used in research backtests."""
        return self.fee_pct if self.fee_pct is not None else DEFAULT_FEE_PCT[self.kind]


@dataclass(frozen=True, slots=True)
class PortfolioConfig:
    """A whole portfolio: settings, risk limits, instruments and sleeves."""

    name: str
    instruments: dict[str, InstrumentSpec]
    sleeves: tuple[SleeveSpec, ...]
    base_currency: str = "USD"
    initial_equity: float = 10_000.0
    rebalance_band: float = 0.02
    scale: float = 1.0  # multiplies every allocated target: the one knob that sizes the whole book (see risk_budget.py)
    allocation: str = "equal"
    allocation_lookback_days: int = 90
    allocation_refit_days: int = 30
    risk: PortfolioRiskConfig = field(default_factory=PortfolioRiskConfig)
    path: str | None = None

    @property
    def enabled_sleeves(self) -> tuple[SleeveSpec, ...]:
        """Sleeves that trade."""
        return tuple(sleeve for sleeve in self.sleeves if sleeve.enabled)

    def budgets(self) -> dict[str, float]:
        """Enabled sleeve id to budget."""
        return {sleeve.id: sleeve.budget for sleeve in self.enabled_sleeves}

    def venues(self) -> dict[str, str]:
        """Instrument id to venue."""
        return {instrument_id: spec.venue for instrument_id, spec in self.instruments.items()}

    def can_short(self) -> dict[str, bool]:
        """Instrument id to whether it may be short."""
        return {instrument_id: spec.can_short for instrument_id, spec in self.instruments.items()}


_PORTFOLIO_KEYS = {"name", "base_currency", "initial_equity", "rebalance_band", "scale", "allocation", "allocation_lookback_days", "allocation_refit_days"}
_TOP_KEYS = {"portfolio", "risk", "instruments", "sleeves"}


def _known(cls: type) -> set[str]:
    return {item.name for item in fields(cls)}


def _number(table: dict[str, Any], key: str, default: float, label: str, errors: list[str]) -> float | None:
    """`table[key]` as a float, or None after recording an error (a quoted "10000" must not crash the loader)."""
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{label} {key} must be a number, not {value!r}")
        return None
    return float(value)


def parse_portfolio_config(raw: dict[str, Any], *, path: str | None = None) -> PortfolioConfig:
    """Validate a parsed TOML document and build the config, or raise listing every problem."""
    from src.research.catalog import build_strategy
    from src.risk.sizing import build_sizer

    errors: list[str] = []
    for key in sorted(set(raw) - _TOP_KEYS):
        errors.append(f"unknown top-level section [{key}]; allowed: {sorted(_TOP_KEYS)}")

    portfolio = dict(raw.get("portfolio", {}))
    for key in sorted(set(portfolio) - _PORTFOLIO_KEYS):
        errors.append(f"[portfolio] unknown key '{key}'; allowed: {sorted(_PORTFOLIO_KEYS)}")
    if portfolio.get("allocation", "equal") not in ALLOCATION_METHODS:
        errors.append(f"[portfolio] allocation must be one of {list(ALLOCATION_METHODS)}")
    initial_equity = _number(portfolio, "initial_equity", 10_000.0, "[portfolio]", errors)
    if initial_equity is not None and initial_equity <= 0:
        errors.append("[portfolio] initial_equity must be above 0")
    rebalance_band = _number(portfolio, "rebalance_band", 0.02, "[portfolio]", errors)
    if rebalance_band is not None and not 0 <= rebalance_band < 1:
        errors.append("[portfolio] rebalance_band must be between 0 and 1 (0.02 = 2% of equity)")
    scale = _number(portfolio, "scale", 1.0, "[portfolio]", errors)
    if scale is not None and not 0 < scale <= 10:
        errors.append("[portfolio] scale must be above 0 and at most 10 (0.5 = half the size the sleeves ask for)")
    for key, default in (("allocation_lookback_days", 90), ("allocation_refit_days", 30)):
        value = portfolio.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            errors.append(f"[portfolio] {key} must be a whole number of at least 1")

    risk = PortfolioRiskConfig()
    risk_raw = dict(raw.get("risk", {}))
    unknown_risk = sorted(set(risk_raw) - _known(PortfolioRiskConfig))
    for key in unknown_risk:
        errors.append(f"[risk] unknown key '{key}'; allowed: {sorted(_known(PortfolioRiskConfig))}")
    if not unknown_risk:
        try:
            risk = PortfolioRiskConfig(**risk_raw)
        except (TypeError, ValueError) as exc:
            errors.append(f"[risk] {exc}")

    instruments: dict[str, InstrumentSpec] = {}
    for instrument_id, table in dict(raw.get("instruments", {})).items():
        label = f'[instruments."{instrument_id}"]'
        if ":" not in instrument_id:
            errors.append(f'{label} id must be "venue:symbol", e.g. "kraken_futures:BTC/USD"')
            continue
        unknown = sorted(set(table) - (_known(InstrumentSpec) - {"id"}))
        for key in unknown:
            errors.append(f"{label} unknown key '{key}'; allowed: {sorted(_known(InstrumentSpec) - {'id'})}")
        if table.get("kind", "perp") not in DEFAULT_FEE_PCT:
            errors.append(f"{label} kind must be 'perp' or 'spot'")
            continue
        if not unknown:
            instruments[instrument_id] = InstrumentSpec(id=instrument_id, **table)
    if not instruments:
        errors.append('no instruments: add at least one [instruments."venue:symbol"] table')

    sleeves: list[SleeveSpec] = []
    seen: set[str] = set()
    allowed_sleeve_keys = _known(SleeveSpec)
    for position, table in enumerate(raw.get("sleeves", []), start=1):
        sleeve_id = str(table.get("id", f"#{position}"))
        label = f"[[sleeves]] '{sleeve_id}'"
        for key in sorted(set(table) - allowed_sleeve_keys):
            errors.append(f"{label} unknown key '{key}'; allowed: {sorted(allowed_sleeve_keys)}")
        missing = [key for key in ("id", "instrument", "interval", "strategy") if key not in table]
        if missing:
            errors.append(f"{label} is missing {missing}")
            continue
        if not _SLUG.match(sleeve_id):
            errors.append(f"{label} id must be lowercase letters, digits and underscores")
        if sleeve_id in seen:
            errors.append(f"{label} id is used twice")
        seen.add(sleeve_id)
        instrument = instruments.get(table["instrument"])
        if instrument is None:
            errors.append(f"{label} instrument '{table['instrument']}' has no [instruments] table")
        if table["interval"] not in INTERVALS:
            errors.append(f"{label} interval must be one of {list(INTERVALS)}")
        budget = _number(table, "budget", 1.0, label, errors)
        if budget is not None and budget <= 0:
            errors.append(f"{label} budget must be above 0")
        if instrument is not None and not instrument.can_short and not table.get("long_only", False):
            errors.append(f"{label} can go short but {instrument.id} can't; set long_only = true")
        try:
            build_strategy(table["strategy"], **dict(table.get("params", {})), long_only=bool(table.get("long_only", False)))
        except Exception as exc:  # noqa: BLE001 - report any strategy construction problem as a config error
            errors.append(f"{label} strategy: {exc}")
        try:
            build_sizer(table.get("sizing", "fixed_fraction"), **dict(table.get("sizing_params", {"fraction": 1.0} if table.get("sizing", "fixed_fraction") == "fixed_fraction" else {})))
        except (TypeError, ValueError) as exc:
            errors.append(f"{label} sizing: {exc}")
        for key in sorted(set(table.get("stops", {})) - set(STOP_KEYS)):
            errors.append(f"{label} stops: unknown key '{key}'; allowed: {list(STOP_KEYS)}")
        if not set(table) - allowed_sleeve_keys and budget is not None:
            values = dict(table)
            if "sizing_params" not in values and values.get("sizing", "fixed_fraction") != "fixed_fraction":
                values["sizing_params"] = {}
            sleeves.append(SleeveSpec(**values))
    if not any(sleeve.enabled for sleeve in sleeves) and not errors:
        errors.append("no enabled sleeves: add a [[sleeves]] block")
    if portfolio.get("allocation", "equal") == "fixed":
        total = sum(sleeve.budget for sleeve in sleeves if sleeve.enabled)
        if total > 1.0 + 1e-9:
            errors.append(f"[portfolio] allocation = 'fixed' needs sleeve budgets summing to at most 1 (they sum to {total:g})")

    if errors:
        source = f" in {path}" if path else ""
        raise PortfolioConfigError(f"{len(errors)} problem(s){source}:\n" + "\n".join(f"  - {error}" for error in errors))
    return PortfolioConfig(
        name=str(portfolio.get("name", Path(path).stem if path else "portfolio")),
        instruments=instruments,
        sleeves=tuple(sleeves),
        base_currency=str(portfolio.get("base_currency", "USD")),
        initial_equity=float(portfolio.get("initial_equity", 10_000.0)),
        rebalance_band=float(portfolio.get("rebalance_band", 0.02)),
        scale=float(portfolio.get("scale", 1.0)),
        allocation=str(portfolio.get("allocation", "equal")),
        allocation_lookback_days=int(portfolio.get("allocation_lookback_days", 90)),
        allocation_refit_days=int(portfolio.get("allocation_refit_days", 30)),
        risk=risk,
        path=path,
    )


def load_portfolio_config(path: str | Path) -> PortfolioConfig:
    """Read and validate a portfolio TOML file."""
    location = Path(path)
    if not location.exists():
        raise PortfolioConfigError(f"portfolio config not found: {location}")
    try:
        raw = tomllib.loads(location.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise PortfolioConfigError(f"{location} is not valid TOML: {exc}") from exc
    return parse_portfolio_config(raw, path=str(location))


def describe(config: PortfolioConfig) -> str:
    """A readable summary of the resolved config, for `--portfolio-check`."""
    from src.portfolio.allocation import sleeve_scales

    lines = [f"Portfolio '{config.name}' ({config.path or 'in memory'}): base {config.base_currency}, initial equity {config.initial_equity:,.0f}, "
             f"allocation {config.allocation}, rebalance band {config.rebalance_band:.1%}" + (f", scale {config.scale:g}" if config.scale != 1.0 else "")]
    lines.append("Instruments:")
    for spec in config.instruments.values():
        lines.append(f"  {spec.id:<28} {spec.kind:<4} short={'yes' if spec.can_short else 'no':<3} max leverage {spec.max_leverage:g}x, fee {spec.taker_fee_pct:.2f}%, slippage {spec.slippage_bps:g} bps")
    scales = sleeve_scales(config.budgets(), config.allocation) if config.allocation != "inverse_vol" else {}
    lines.append("Sleeves:")
    for sleeve in config.sleeves:
        # The scale multiplies the sleeve's own target (which its sizer may put above 1), so it isn't a share of equity.
        if not sleeve.enabled:
            share = "not traded"
        elif sleeve.id in scales:
            share = f"scale {scales[sleeve.id]:.3g}"
        else:
            share = "scale set by volatility"
        state = "" if sleeve.enabled else "  [disabled]"
        params = ", ".join(f"{k}={v}" for k, v in sleeve.params.items())
        lines.append(f"  {sleeve.id:<18} {sleeve.instrument:<24} {sleeve.interval:<3} {sleeve.strategy}({params}){' long-only' if sleeve.long_only else ''}; "
                     f"sizing {sleeve.sizing} {sleeve.sizing_params}; budget {sleeve.budget:g} -> {share}{'; stops ' + str(sleeve.stops) if sleeve.stops else ''}{state}")
    risk = config.risk
    venues = f", venues {risk.max_venue_exposure}" if risk.max_venue_exposure else ""
    derisk = (f"de-risk from {risk.drawdown_derisk_start:.0%} drawdown to {risk.drawdown_derisk_floor:.0%} size at {risk.max_drawdown:.0%}, then flatten"
              if risk.drawdown_derisk_start is not None else f"flatten at {risk.max_drawdown:.0%} drawdown")
    daily = f"daily loss limit {risk.daily_loss_limit:.0%}" if risk.daily_loss_limit is not None else "no daily loss limit"
    lines.append(f"Risk: gross <= {risk.max_gross_exposure:g}x, net <= {risk.max_net_exposure:g}x, per instrument <= {risk.max_instrument_weight:g}x{venues}; {derisk}; {daily}")
    return "\n".join(lines)
