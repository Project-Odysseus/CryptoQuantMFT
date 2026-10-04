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
from src.portfolio.basket import BasketSpec
from src.portfolio.review import ReviewConfig
from src.portfolio.risk import PortfolioRiskConfig
from src.portfolio.sleeves import PAUSE_KEY, STOP_KEYS, SleeveSpec
from src.runtime.config import BAR_INTERVALS

INTERVALS = tuple(BAR_INTERVALS)  # the runtime's bar lengths, so a config never asks for one it can't build
_SLUG = re.compile(r"^[a-z0-9_]+$")
DEFAULT_FEE_PCT = {"perp": 0.05, "spot": 0.40,  # Kraken taker, entry tier
                   "option": 0.03}  # Deribit: 0.03% of the underlying per option (capped at 12.5% of its price)


@dataclass(frozen=True, slots=True)
class ContractSleeveSpec:
    """One `[[contract_sleeves]]` block: a strategy that holds contracts on one venue under a loss budget.

    Attributes:
        id: Unique among all sleeves.
        venue: "kalshi" or "polymarket"; the sleeve's cash sits there.
        strategy: A name registered in `src/portfolio/contracts.py`.
        budget: Share of the book's equity the sleeve may have at risk (the cost of its bets), e.g. 0.05.
        max_event_share: The most of that budget one event may take (markets of one event win and lose together).
        params: Passed to the strategy.
    """

    id: str
    venue: str
    strategy: str
    budget: float
    max_event_share: float = 1.0
    params: dict[str, Any] = field(default_factory=dict)


CONTRACT_VENUES = ("kalshi", "polymarket")


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
        group: A name shared by instruments that are capped together
            (`[risk.groups.<name>]`), e.g. "majors" or "alts".
    """

    id: str
    kind: str = "perp"
    allow_short: bool | None = None
    max_leverage: float = 1.0
    fee_pct: float | None = None
    slippage_bps: float = 5.0
    min_order_size: float = 0.0
    lot_step: float = 0.0
    group: str | None = None

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

    @property
    def underlying(self) -> str:
        """The coin the instrument moves with ("BTC" for BTC/USD and XBT/EUR): one exposure across spot and perp."""
        from src.portfolio.exposure_limits import base_coin

        return base_coin(self.symbol)


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
    # Below this equity (base currency; 0 = off), a nonzero target that rounds to zero lots holds one lot, if that
    # lot is at most `small_account_max_lot_weight` of equity. For accounts where one lot is a big share of the book.
    small_account_equity: float = 0.0
    small_account_max_lot_weight: float = 0.9
    allocation: str = "equal"
    allocation_lookback_days: int = 90
    allocation_refit_days: int = 30
    risk: PortfolioRiskConfig = field(default_factory=PortfolioRiskConfig)
    review: ReviewConfig | None = None  # kill criteria, set before live ([review]; src/portfolio/review.py)
    baskets: tuple[BasketSpec, ...] = ()  # [[baskets]]: their member sleeves are part of `sleeves`
    # [execution] policy: "taker" sends every order as a market order. "maker_first" first rests it at the touch as a
    # post-only limit order (no spread paid, the lower maker fee) and, if it hasn't filled after
    # `maker_timeout_seconds`, cancels it and sends what is left as a market order. Checked once per runtime cycle.
    contract_sleeves: tuple[ContractSleeveSpec, ...] = ()  # [[contract_sleeves]]: bets under a loss budget (src/portfolio/contracts.py)
    execution_policy: str = "taker"
    maker_timeout_seconds: float = 120.0
    path: str | None = None

    def small_lot_cap(self, equity: float) -> float | None:
        """The one-lot allowance while the account is below `small_account_equity`, within the money cap; else None."""
        if not 0 < equity < self.small_account_equity:
            return None
        cap = self.small_account_max_lot_weight
        if self.risk.max_gross_notional is not None:
            cap = min(cap, self.risk.max_gross_notional / equity)
        return cap

    @property
    def enabled_sleeves(self) -> tuple[SleeveSpec, ...]:
        """Sleeves that trade."""
        return tuple(sleeve for sleeve in self.sleeves if sleeve.enabled)

    def budgets(self) -> dict[str, float]:
        """Enabled sleeve id to budget."""
        return {sleeve.id: sleeve.budget for sleeve in self.enabled_sleeves}

    def allocation_groups(self) -> dict[str, str]:
        """Enabled sleeve id -> the unit allocation treats it as: itself, or its basket (a basket counts as one sleeve)."""
        return {sleeve.id: sleeve.basket or sleeve.id for sleeve in self.enabled_sleeves}

    def group_budgets(self) -> dict[str, float]:
        """Allocation unit -> budget: each plain sleeve's own, each basket's once."""
        out: dict[str, float] = {}
        for sleeve in self.enabled_sleeves:
            out.setdefault(sleeve.basket or sleeve.id, sleeve.budget)
        return out

    def basket(self, basket_id: str) -> BasketSpec:
        """The basket with this id."""
        return next(basket for basket in self.baskets if basket.id == basket_id)

    def venues(self) -> dict[str, str]:
        """Instrument id to venue."""
        return {instrument_id: spec.venue for instrument_id, spec in self.instruments.items()}

    def can_short(self) -> dict[str, bool]:
        """Instrument id to whether it may be short."""
        return {instrument_id: spec.can_short for instrument_id, spec in self.instruments.items()}

    def groups(self) -> dict[str, str]:
        """Instrument id to its group, for the instruments that have one."""
        return {instrument_id: spec.group for instrument_id, spec in self.instruments.items() if spec.group}

    def underlyings(self) -> dict[str, str]:
        """Instrument id to the coin it moves with."""
        return {instrument_id: spec.underlying for instrument_id, spec in self.instruments.items()}

    def traded_instruments(self) -> list[str]:
        """The instruments enabled sleeves trade, in a fixed order (the risk model's)."""
        return sorted({sleeve.instrument for sleeve in self.enabled_sleeves})


_PORTFOLIO_KEYS = {"name", "base_currency", "initial_equity", "rebalance_band", "scale", "allocation", "allocation_lookback_days", "allocation_refit_days",
                   "small_account_equity", "small_account_max_lot_weight"}
_TOP_KEYS = {"portfolio", "risk", "instruments", "sleeves", "review", "baskets", "execution", "contract_sleeves"}
_EXECUTION_KEYS = {"policy", "maker_timeout_seconds"}
EXECUTION_POLICIES = ("taker", "maker_first")


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
    small_account_equity = _number(portfolio, "small_account_equity", 0.0, "[portfolio]", errors)
    if small_account_equity is not None and small_account_equity < 0:
        errors.append("[portfolio] small_account_equity must be 0 (off) or above")
    small_lot_weight = _number(portfolio, "small_account_max_lot_weight", 0.9, "[portfolio]", errors)
    if small_lot_weight is not None and not 0 < small_lot_weight <= 1:
        errors.append("[portfolio] small_account_max_lot_weight must be above 0 and at most 1 (0.9 = one lot up to 90% of equity)")
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
        if table.get("kind", "perp") not in ("perp", "spot"):
            errors.append(f"{label} kind must be 'perp' or 'spot' (option contracts are added by option sleeves, not listed here)")
            continue
        if not unknown:
            instruments[instrument_id] = InstrumentSpec(id=instrument_id, **table)
    if not instruments:
        errors.append('no instruments: add at least one [instruments."venue:symbol"] table')

    sleeves: list[SleeveSpec] = []
    seen: set[str] = set()
    allowed_sleeve_keys = _known(SleeveSpec) - {"basket"}
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
        pause = dict(table.get("stops", {})).get(PAUSE_KEY)
        if pause is not None and (isinstance(pause, bool) or not isinstance(pause, (int, float)) or not 0 < pause < 1):
            errors.append(f"{label} stops: {PAUSE_KEY} must be above 0 and below 1 (0.2 = pause after losing 20% from the sleeve's peak)")
        if not set(table) - allowed_sleeve_keys and budget is not None:
            values = dict(table)
            if "sizing_params" not in values and values.get("sizing", "fixed_fraction") != "fixed_fraction":
                values["sizing_params"] = {}
            sleeves.append(SleeveSpec(**values))
    baskets: list[BasketSpec] = []
    for position, table in enumerate(raw.get("baskets", []), start=1):
        basket_id = str(table.get("id", f"#{position}"))
        label = f"[[baskets]] '{basket_id}'"
        unknown = sorted(set(table) - _known(BasketSpec))
        for key in unknown:
            errors.append(f"{label} unknown key '{key}'; allowed: {sorted(_known(BasketSpec))}")
        if unknown:
            continue
        try:
            basket = BasketSpec(**{**table, "coins": tuple(str(coin).upper() for coin in table.get("coins", []))})
        except (TypeError, ValueError) as exc:
            errors.append(f"{label} {exc}")
            continue
        if basket.id in seen:
            errors.append(f"{label} id is used by a sleeve or another basket")
        seen.add(basket.id)
        for member_id, (coin, instrument_id) in basket.members().items():
            instrument = instruments.get(instrument_id)
            if instrument is None or instrument.kind != "perp":
                errors.append(f"{label} coin {coin} needs a perp [instruments.\"{instrument_id}\"] table (scripts/portfolio/basket_instruments.py writes them)")
            elif not basket.long_only and not instrument.can_short:
                errors.append(f"{label} is long/short but {instrument_id} can't be shorted")
            if member_id in seen:
                errors.append(f"{label} member sleeve '{member_id}' clashes with another sleeve")
            seen.add(member_id)
            sleeves.append(SleeveSpec(id=member_id, instrument=instrument_id, interval=basket.interval, strategy="basket", budget=basket.budget,
                                      warmup_bars=0, enabled=basket.enabled, basket=basket.id))
        baskets.append(basket)
    if baskets and portfolio.get("allocation", "equal") == "inverse_vol":
        errors.append("[portfolio] allocation can't be 'inverse_vol' in a portfolio with baskets (it sizes by one instrument's volatility); "
                      "use fixed, equal, risk_parity or hrp")
    used_groups = {spec.group for spec in instruments.values() if spec.group}
    for group in sorted(set(risk.groups) - used_groups):
        errors.append(f"[risk.groups.{group}] has no instruments: add group = \"{group}\" to an [instruments] table")
    traded = {sleeve.instrument for sleeve in sleeves if sleeve.enabled}
    if risk.benchmark is not None and risk.benchmark not in traded:
        errors.append(f"[risk] benchmark '{risk.benchmark}' must be an instrument an enabled sleeve trades (its bars feed the risk model)")
    if not any(sleeve.enabled for sleeve in sleeves) and not errors:
        errors.append("no enabled sleeves: add a [[sleeves]] block")
    if portfolio.get("allocation", "equal") == "fixed":
        total = sum(sleeve.budget for sleeve in sleeves if sleeve.enabled)
        if total > 1.0 + 1e-9:
            errors.append(f"[portfolio] allocation = 'fixed' needs sleeve budgets summing to at most 1 (they sum to {total:g})")

    review = None
    review_raw = dict(raw.get("review", {}) or {})
    if review_raw:
        unknown_review = sorted(set(review_raw) - _known(ReviewConfig))
        for key in unknown_review:
            errors.append(f"[review] unknown key '{key}'; allowed: {sorted(_known(ReviewConfig))}")
        sleeve_ids = {sleeve.id for sleeve in sleeves}
        for sleeve_id in sorted(set(dict(review_raw.get("backtest_sharpe", {}))) - sleeve_ids):
            errors.append(f"[review] backtest_sharpe names '{sleeve_id}', which is not a sleeve")
        if not unknown_review:
            try:
                review = ReviewConfig(**{**review_raw, "backtest_sharpe": {k: float(v) for k, v in dict(review_raw.get("backtest_sharpe", {})).items()}})
            except (TypeError, ValueError) as exc:
                errors.append(f"[review] {exc}")

    contract_sleeves: list[ContractSleeveSpec] = []
    taken = {sleeve.id for sleeve in sleeves}
    for position, table in enumerate(raw.get("contract_sleeves", []), start=1):
        from src.portfolio.contracts import REGISTRY

        label = f"[[contract_sleeves]] #{position}"
        table = dict(table)
        for key in sorted(set(table) - {"id", "venue", "strategy", "budget", "params", "max_event_share"}):
            errors.append(f"{label} unknown key '{key}'")
        sleeve_id = str(table.get("id", ""))
        if not sleeve_id or sleeve_id in taken:
            errors.append(f"{label} needs an id that no other sleeve uses (got {sleeve_id!r})")
        taken.add(sleeve_id)
        if table.get("venue") not in CONTRACT_VENUES:
            errors.append(f"{label} venue must be one of {list(CONTRACT_VENUES)}, not {table.get('venue')!r}")
        if table.get("strategy") not in REGISTRY:
            errors.append(f"{label} strategy '{table.get('strategy')}' is not registered (registered: {sorted(REGISTRY) or 'none yet'})")
        budget = _number(table, "budget", 0.0, label, errors)
        if budget is not None and not 0 < budget <= 0.5:
            errors.append(f"{label} budget must be above 0 and at most 0.5 (a share of equity; 0.05 = 5%)")
        event_share = _number(table, "max_event_share", 1.0, label, errors)
        if event_share is not None and not 0 < event_share <= 1:
            errors.append(f"{label} max_event_share must be above 0 and at most 1 (a share of the sleeve's budget)")
        if not errors or all(label not in error for error in errors):
            contract_sleeves.append(ContractSleeveSpec(id=sleeve_id, venue=str(table["venue"]), strategy=str(table["strategy"]), budget=float(budget),
                                                       max_event_share=float(event_share), params=dict(table.get("params", {}))))
    if sum(sleeve.budget for sleeve in contract_sleeves) > 0.5:
        errors.append("[[contract_sleeves]] budgets add up to more than half of equity")

    execution = dict(raw.get("execution", {}) or {})
    for key in sorted(set(execution) - _EXECUTION_KEYS):
        errors.append(f"[execution] unknown key '{key}'; allowed: {sorted(_EXECUTION_KEYS)}")
    if execution.get("policy", "taker") not in EXECUTION_POLICIES:
        errors.append(f"[execution] policy must be one of {list(EXECUTION_POLICIES)}, not {execution.get('policy')!r}")
    maker_timeout = _number(execution, "maker_timeout_seconds", 120.0, "[execution]", errors)
    if maker_timeout is not None and not 10 <= maker_timeout <= 3600:
        errors.append("[execution] maker_timeout_seconds must be between 10 and 3600 (an order still resting when the next bar closes blocks that bar's trade)")

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
        small_account_equity=float(portfolio.get("small_account_equity", 0.0)),
        small_account_max_lot_weight=float(portfolio.get("small_account_max_lot_weight", 0.9)),
        allocation=str(portfolio.get("allocation", "equal")),
        allocation_lookback_days=int(portfolio.get("allocation_lookback_days", 90)),
        allocation_refit_days=int(portfolio.get("allocation_refit_days", 30)),
        risk=risk,
        review=review,
        baskets=tuple(baskets),
        contract_sleeves=tuple(contract_sleeves),
        execution_policy=str(execution.get("policy", "taker")),
        maker_timeout_seconds=float(maker_timeout),
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
    groups = config.allocation_groups()
    group_scales = sleeve_scales(config.group_budgets(), config.allocation) if config.allocation in ("fixed", "equal") else {}
    by_risk = "scale set by volatility" if config.allocation == "inverse_vol" else "scale set by its risk and correlations"
    scales = {sleeve_id: group_scales[group] for sleeve_id, group in groups.items() if group in group_scales}
    lines.append("Sleeves:")
    for sleeve in config.sleeves:
        if sleeve.basket is not None:
            continue  # listed once per basket below
        # The scale multiplies the sleeve's own target (which its sizer may put above 1), so it isn't a share of equity.
        if not sleeve.enabled:
            share = "not traded"
        elif sleeve.id in scales:
            share = f"scale {scales[sleeve.id]:.3g}"
        else:
            share = by_risk
        state = "" if sleeve.enabled else "  [disabled]"
        params = ", ".join(f"{k}={v}" for k, v in sleeve.params.items())
        lines.append(f"  {sleeve.id:<18} {sleeve.instrument:<24} {sleeve.interval:<3} {sleeve.strategy}({params}){' long-only' if sleeve.long_only else ''}; "
                     f"sizing {sleeve.sizing} {sleeve.sizing_params}; budget {sleeve.budget:g} -> {share}{'; stops ' + str(sleeve.stops) if sleeve.stops else ''}{state}")
    for basket in config.baskets:
        scale = group_scales.get(basket.id)
        legs = "long only" if basket.long_only else f"long top {basket.quantile:.0%} / short bottom {basket.quantile:.0%}"
        lines.append(f"  {basket.id:<18} basket of {len(basket.coins)} {basket.venue} perps: {basket.signal}, top {basket.top_n} by Binance volume, {legs}, "
                     f"gross {basket.gross:g}, rebalance every {basket.rebalance_days} days; budget {basket.budget:g}"
                     + (f" -> scale {scale:.3g}" if scale is not None else f" -> {by_risk}") + ("" if basket.enabled else "  [disabled]"))
    risk = config.risk
    venues = f", venues {risk.max_venue_exposure}" if risk.max_venue_exposure else ""
    derisk = (f"de-risk from {risk.drawdown_derisk_start:.0%} drawdown to {risk.drawdown_derisk_floor:.0%} size at {risk.max_drawdown:.0%}, then flatten"
              if risk.drawdown_derisk_start is not None else f"flatten at {risk.max_drawdown:.0%} drawdown")
    daily = f"daily loss limit {risk.daily_loss_limit:.0%}" if risk.daily_loss_limit is not None else "no daily loss limit"
    daily += f"; positions at most {risk.max_gross_notional:,.0f} {config.base_currency} in total" if risk.max_gross_notional else ""
    lines.append(f"Risk: gross <= {risk.max_gross_exposure:g}x, net <= {risk.max_net_exposure:g}x, per instrument <= {risk.max_instrument_weight:g}x{venues}; {derisk}; {daily}")
    extra = [f"group {group}: " + ", ".join(f"{key.removeprefix('max_')} <= {value:g}x" for key, value in limits.items())
             + f" ({sum(1 for spec in config.instruments.values() if spec.group == group)} instruments)" for group, limits in risk.groups.items()]
    if risk.max_beta_exposure is not None:
        extra.append(f"beta to {risk.benchmark} <= {risk.max_beta_exposure:g}x")
    if risk.max_portfolio_vol is not None:
        stress = f"correlations floored at {risk.stress_correlation:g}" if risk.stress_correlation > 0 else "estimated correlations"
        extra.append(f"book volatility <= {risk.max_portfolio_vol:.0%} a year ({stress}, {risk.vol_halflife_days:g}-day half-life)")
    extra += [f"{key.removeprefix('max_')} <= {value:g}" for key, value in risk.exposure.items()]
    if risk.max_average_correlation is not None or risk.min_effective_bets is not None:
        extra.append("alert when the sleeves' average correlation is above " + (f"{risk.max_average_correlation:g}" if risk.max_average_correlation is not None else "-")
                     + " or their effective bets below " + (f"{risk.min_effective_bets:g}" if risk.min_effective_bets is not None else "-"))
    if extra:
        lines.append("Exposure limits: " + "; ".join(extra))
    stop = (f"a reduce-only stop resting on the exchange {risk.exchange_stop_pct:.0%} beyond each position's price" if risk.exchange_stop_pct is not None
            else "no exchange stop (live: nothing caps the loss if the process dies)")
    cooldown = (f"after {risk.rejection_cooldown_after} rejected orders in a row, only reductions on that instrument for {risk.rejection_cooldown_hours:g}h"
                if risk.rejection_cooldown_after else "no rejection cooldown")
    lines.append(f"Safety: {stop}; {cooldown}")
    if config.review is not None:
        review = config.review
        refs = ", ".join(f"{sleeve} {value:g}" for sleeve, value in review.backtest_sharpe.items())
        lines.append(f"Kill criteria: flag a Sharpe {review.decay_z:g} standard errors below the backtest's ({refs}; book {review.book_backtest_sharpe}) "
                     f"after {review.min_days} days, a negative one after {review.floor_days}, a drawdown beyond {review.max_drawdown_multiple:g}x "
                     f"{review.book_backtest_max_drawdown}")
    else:
        lines.append("Kill criteria: none ([review]; set them before going live)")
    return "\n".join(lines)
