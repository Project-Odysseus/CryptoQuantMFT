"""The instrument master: one definition per tradable thing or research series (plan 1.1).

An instrument says what it is (asset class, kind), where and when it trades (venue, calendar, timezone, session
close), how its P&L converts (currency, multiplier, tick), what trading it costs, what carry its price series does not
already reflect, where its data comes from (one source per bar interval, "adapter:symbol"), and for listed futures
how its contracts roll. A research series that stands in for a tradable one (an index for its future) names that
instrument in `proxy_for`, and every report flags it.

The master is a TOML file with one `[instruments."<id>"]` table each, parsed strictly (quantlab.config):

    [instruments."cme:NQ"]
    name = "E-mini Nasdaq-100"
    asset_class = "equity_index"
    kind = "future"
    venue = "CME"
    currency = "USD"
    calendar = "XNYS"
    timezone = "America/New_York"
    session_close = "16:00"
    multiplier = 20.0
    tick_size = 0.25
    cost_bps = 1.5
    sources = { "1d" = "yahoo:NQ=F" }
    contract = { root = "NQ", months = "HMUZ", last_trade_rule = "third_friday", roll_sessions_before = 3 }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal, Mapping

from quantlab.config import ConfigError, load, parse

AssetClass = Literal["equity_index", "rates", "fx", "commodity", "crypto", "volatility", "credit", "other"]
Kind = Literal["future", "index", "spot", "perp", "etf", "rate", "fx_rate"]
LAST_TRADE_RULES = ("third_friday", "two_sessions_before_third_wednesday", "one_session_before_third_wednesday", "thursday_before_second_friday",
                    "last_session_of_month", "first_notice_prior_month", "crude_oil")


@dataclass(frozen=True)
class ContractSpec:
    """How a listed future's contracts are named, expire and roll."""

    root: str
    months: str = "HMUZ"  # contract months as month codes
    last_trade_rule: str = "third_friday"
    roll_sessions_before: int = 3  # move to the next contract this many sessions before the last trading day
    settlement: Literal["cash", "physical"] = "cash"

    def __post_init__(self) -> None:
        if self.last_trade_rule not in LAST_TRADE_RULES:
            raise ValueError(f"last_trade_rule must be one of {LAST_TRADE_RULES}")
        if not self.months or any(code not in "FGHJKMNQUVXZ" for code in self.months):
            raise ValueError(f"months {self.months!r} must be month codes (F G H J K M N Q U V X Z)")
        if self.settlement == "physical" and self.roll_sessions_before < 2:
            raise ValueError("a physically settled contract must roll at least 2 sessions before its last trading day")


@dataclass(frozen=True)
class Instrument:
    """One instrument (see the module docstring)."""

    id: str
    name: str
    asset_class: AssetClass
    kind: Kind
    currency: str
    calendar: str = "weekdays"  # an exchange_calendars code, "weekdays" or "always"
    timezone: str = "UTC"
    session_close: str = "16:00"  # local time when the daily bar is final
    venue: str = ""
    multiplier: float = 1.0
    tick_size: float = 0.0
    cost_bps: float = 1.0  # per side: half the spread plus commission, in basis points of the traded notional
    cost_ticks: float = 0.0  # per side in ticks, for contracts quoted that way (used instead of cost_bps when set)
    carry_yield: float = 0.0  # yearly carry the price series does not reflect (an index's dividend yield)
    rate_area: str = ""  # whose short rate funds it (for excess returns of proxies)
    carry_area: str = ""  # whose short rate holding it earns (a currency's), for an FX spot proxy of a currency future
    proxy_for: str = ""  # the tradable instrument this series stands in for
    sources: dict[str, str] = field(default_factory=dict)  # bar interval -> "adapter:symbol"
    contract: ContractSpec | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if self.multiplier <= 0:
            raise ValueError(f"{self.id}: multiplier must be positive")
        if self.cost_bps < 0 or self.cost_ticks < 0:
            raise ValueError(f"{self.id}: costs must not be negative")
        if self.cost_ticks and not self.tick_size:
            raise ValueError(f"{self.id}: cost_ticks needs a tick_size")
        if self.kind == "future" and self.contract is None and not self.proxy_for:
            raise ValueError(f"{self.id}: a future needs a contract spec")
        for interval, source in self.sources.items():
            if ":" not in source:
                raise ValueError(f"{self.id}: source for {interval} must be 'adapter:symbol', got {source!r}")

    def source(self, interval: str) -> tuple[str, str]:
        """(adapter, symbol) for a bar interval."""
        if interval not in self.sources:
            raise KeyError(f"{self.id} has no {interval} source; it has {sorted(self.sources)}")
        adapter, symbol = self.sources[interval].split(":", 1)
        return adapter, symbol

    @property
    def is_proxy(self) -> bool:
        return bool(self.proxy_for)


@dataclass(frozen=True)
class _MasterFile:
    instruments: dict[str, dict] = field(default_factory=dict)


class InstrumentMaster:
    """All instruments by id, loaded from one or more TOML files."""

    def __init__(self, instruments: Mapping[str, Instrument]) -> None:
        self._instruments = dict(instruments)
        missing = sorted({item.proxy_for for item in self._instruments.values() if item.proxy_for} - set(self._instruments))
        if missing:
            raise ConfigError([f"proxy_for names unknown instruments: {missing}"])

    @classmethod
    def load(cls, *paths: str | Path) -> "InstrumentMaster":
        """Parse every file; ids must be unique across files."""
        instruments: dict[str, Instrument] = {}
        problems: list[str] = []
        for path in paths:
            raw = load(path, _MasterFile).instruments
            for instrument_id, table in raw.items():
                if instrument_id in instruments:
                    problems.append(f"{path}: {instrument_id} is defined twice")
                    continue
                try:
                    instruments[instrument_id] = parse({"id": instrument_id, **table}, Instrument)
                except ConfigError as exc:
                    problems += [f"{path}: instruments.{instrument_id}.{problem}" for problem in exc.problems]
        if problems:
            raise ConfigError(problems)
        return cls(instruments)

    def __getitem__(self, instrument_id: str) -> Instrument:
        try:
            return self._instruments[instrument_id]
        except KeyError:
            raise KeyError(f"unknown instrument {instrument_id!r}") from None

    def __contains__(self, instrument_id: object) -> bool:
        return instrument_id in self._instruments

    def __iter__(self) -> Iterator[Instrument]:
        return iter(self._instruments.values())

    def __len__(self) -> int:
        return len(self._instruments)

    def select(self, *, asset_class: str | None = None, kind: str | None = None, interval: str | None = None) -> list[Instrument]:
        """Instruments matching every given filter (an interval filter keeps those with a source for it)."""
        return [item for item in self if (asset_class is None or item.asset_class == asset_class) and (kind is None or item.kind == kind)
                and (interval is None or interval in item.sources)]
