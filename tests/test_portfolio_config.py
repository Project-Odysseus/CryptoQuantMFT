"""Portfolio config (src/portfolio/config.py): the example loads, and every validation rule names what to fix."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.portfolio.config import PortfolioConfigError, describe, load_portfolio_config, parse_portfolio_config

EXAMPLE = "config/portfolio.example.toml"
BASE: dict[str, Any] = {
    "portfolio": {"name": "unit", "allocation": "equal"},
    "risk": {"max_gross_exposure": 1.5},
    "instruments": {"kraken_futures:BTC/USD": {"kind": "perp"}, "kraken:BTC/EUR": {"kind": "spot"}},
    "sleeves": [
        {"id": "btc_ma", "instrument": "kraken_futures:BTC/USD", "interval": "1d", "strategy": "moving_average_crossover",
         "params": {"short_window": 4, "long_window": 48}},
        {"id": "spot_ma", "instrument": "kraken:BTC/EUR", "interval": "4h", "strategy": "moving_average_crossover", "long_only": True},
    ],
}


def _parse(**changes: Any) -> Any:
    """BASE with `changes` applied: a key "sleeve0" / "sleeve1" updates that sleeve, other keys update that section."""
    raw = copy.deepcopy(BASE)
    for key, value in changes.items():
        if key in ("sleeve0", "sleeve1"):
            raw["sleeves"][int(key[-1])].update(value)
        elif value is None:
            raw.pop(key)
        else:
            raw.setdefault(key, {}).update(value) if isinstance(value, dict) else raw.__setitem__(key, value)
    return parse_portfolio_config(raw)


def _problems(**changes: Any) -> str:
    with pytest.raises(PortfolioConfigError) as caught:
        _parse(**changes)
    return str(caught.value)


def test_the_example_config_loads_and_describes_itself() -> None:
    config = load_portfolio_config(EXAMPLE)
    assert config.name == "trend-core" and config.allocation == "equal" and config.path == EXAMPLE
    assert list(config.instruments) == ["kraken_futures:BTC/USD", "kraken_futures:ETH/USD"]
    assert [sleeve.id for sleeve in config.enabled_sleeves] == ["btc_ma_1d", "eth_ma_1d", "btc_keltner_ls", "eth_keltner_ls", "btc_ma_4h"]
    assert config.risk.max_drawdown == 0.40 and config.risk.max_venue_exposure == {"kraken_futures": 1.5}
    assert config.venues()["kraken_futures:ETH/USD"] == "kraken_futures" and all(config.can_short().values())

    text = describe(config)
    for sleeve in config.sleeves:
        assert sleeve.id in text
    assert "scale 0.2" in text and "flatten at 40% drawdown" in text and "daily loss limit 6%" in text


def test_defaults_fill_in_what_the_file_leaves_out() -> None:
    config = _parse()
    perp, spot = config.instruments["kraken_futures:BTC/USD"], config.instruments["kraken:BTC/EUR"]
    assert (perp.can_short, perp.taker_fee_pct, perp.venue, perp.symbol) == (True, 0.05, "kraken_futures", "BTC/USD")
    assert (spot.can_short, spot.taker_fee_pct) == (False, 0.40)
    first = config.sleeves[0]
    assert (first.sizing, first.sizing_params, first.budget, first.enabled) == ("fixed_fraction", {"fraction": 1.0}, 1.0, True)
    assert _parse(sleeve0={"sizing": "vol_target"}).sleeves[0].sizing_params == {}
    assert config.initial_equity == 10_000.0 and config.rebalance_band == 0.02 and config.base_currency == "USD"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sleeve0": {"strategy": "no_such_strategy"}}, "[[sleeves]] 'btc_ma' strategy:"),
        ({"sleeve0": {"params": {"short_window": 4, "long_windw": 48}}}, "does not take ['long_windw']"),
        ({"sleeve1": {"id": "btc_ma"}}, "[[sleeves]] 'btc_ma' id is used twice"),
        ({"sleeve0": {"budgett": 0.5}}, "[[sleeves]] 'btc_ma' unknown key 'budgett'"),
        ({"portfolio": {"rebalance": 0.1}}, "[portfolio] unknown key 'rebalance'"),
        ({"risk": {"max_gross": 1.0}}, "[risk] unknown key 'max_gross'"),
        ({"instruments": {"kraken:ETH/EUR": {"kind": "spot", "leverage": 2}}}, "[instruments.\"kraken:ETH/EUR\"] unknown key 'leverage'"),
        ({"strategies": {}}, "unknown top-level section [strategies]"),
        ({"sleeve1": {"long_only": False}}, "can go short but kraken:BTC/EUR can't; set long_only = true"),
        ({"portfolio": {"allocation": "fixed"}, "sleeve0": {"budget": 0.7}, "sleeve1": {"budget": 0.5}}, "summing to at most 1 (they sum to 1.2)"),
        ({"sleeve0": {"interval": "2h"}}, "interval must be one of"),
        ({"sleeve0": {"sizing": "martingale"}}, "[[sleeves]] 'btc_ma' sizing:"),
        ({"sleeve0": {"sizing": "vol_target", "sizing_params": {"target_vol": 0.5}}}, "does not take ['target_vol']"),
        ({"sleeve0": {"stops": {"atr_stop": 2.0}}}, "stops: unknown key 'atr_stop'"),
        ({"sleeve0": {"instrument": "kraken_futures:SOL/USD"}}, "instrument 'kraken_futures:SOL/USD' has no [instruments] table"),
        ({"sleeve0": {"id": "BTC-MA"}}, "id must be lowercase letters, digits and underscores"),
        ({"sleeve0": {"budget": 0}}, "budget must be above 0"),
        ({"portfolio": {"allocation": "risk_parity"}}, "[portfolio] allocation must be one of"),
        ({"portfolio": {"initial_equity": 0}}, "[portfolio] initial_equity must be above 0"),
        ({"portfolio": {"rebalance_band": 1.5}}, "[portfolio] rebalance_band must be between 0 and 1"),
        ({"portfolio": {"allocation_refit_days": 0}}, "[portfolio] allocation_refit_days must be a whole number of at least 1"),
        ({"portfolio": {"initial_equity": "10k"}}, "[portfolio] initial_equity must be a number"),
        ({"sleeve0": {"budget": "half"}}, "[[sleeves]] 'btc_ma' budget must be a number"),
        ({"risk": {"max_drawdown": 0.1, "drawdown_derisk_start": 0.2}}, "[risk] risk.drawdown_derisk_start must be above 0 and below risk.max_drawdown"),
        ({"instruments": {"kraken:ETH/EUR": {"kind": "option"}}}, "kind must be 'perp' or 'spot'"),
        ({"instruments": {"BTCUSD": {"kind": "perp"}}}, 'id must be "venue:symbol"'),
        ({"sleeves": [{"id": "x", "instrument": "kraken_futures:BTC/USD"}]}, "is missing ['interval', 'strategy']"),
        ({"sleeves": []}, "no enabled sleeves"),
        ({"instruments": None, "sleeves": []}, "no instruments"),
    ],
)
def test_each_mistake_gets_a_message_naming_the_key(changes: dict[str, Any], message: str) -> None:
    assert message in _problems(**changes)


def test_every_problem_is_reported_at_once() -> None:
    text = _problems(portfolio={"allocation": "nope"}, sleeve0={"interval": "2h"}, sleeve1={"id": "btc_ma"})
    assert text.startswith("3 problem(s)")


def test_loading_reports_a_missing_or_malformed_file(tmp_path) -> None:
    with pytest.raises(PortfolioConfigError, match="not found"):
        load_portfolio_config(tmp_path / "missing.toml")
    broken = tmp_path / "broken.toml"
    broken.write_text("[portfolio\nname = 1")
    with pytest.raises(PortfolioConfigError, match="is not valid TOML"):
        load_portfolio_config(broken)
    bad = tmp_path / "bad.toml"
    bad.write_text('[portfolio]\nallocation = "nope"\n')
    with pytest.raises(PortfolioConfigError, match=f"in {bad}"):
        load_portfolio_config(bad)


def test_a_disabled_sleeve_is_kept_but_not_traded() -> None:
    config = _parse(sleeve1={"enabled": False})
    assert [sleeve.id for sleeve in config.enabled_sleeves] == ["btc_ma"] and config.budgets() == {"btc_ma": 1.0}
    line = next(line for line in describe(config).splitlines() if "spot_ma" in line)
    assert "[disabled]" in line and "volatility" not in line
