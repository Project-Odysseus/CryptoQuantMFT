"""The strategy card (src/research/strategy_checks.py): known answers on synthetic return series."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.research import strategy_checks as stc

FAST = stc.StrategyStandard(bootstrap_runs=400, placebo_runs=100)
DAYS = pd.bdate_range("2012-01-02", "2025-12-31")


def _returns(sharpe: float, *, seed: int, days: pd.DatetimeIndex = DAYS, vol: float = 0.10) -> pd.Series:
    """Daily returns at `vol` a year whose true annual Sharpe ratio is `sharpe`."""
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(sharpe * vol / 252.0, vol / np.sqrt(252.0), len(days)), index=days)


def test_the_standard_file_loads_and_rejects_unknown_keys(tmp_path) -> None:
    assert stc.load_standard().version >= 1
    (tmp_path / "bad.toml").write_text("version = 1\nt_mn = 1.0\n")
    with pytest.raises(ValueError, match="unknown keys"):
        stc.load_standard(tmp_path / "bad.toml")


def test_noise_is_no_evidence() -> None:
    card = stc.strategy_card(_returns(0.0, seed=1), name="noise", standard=FAST, ledger=False)
    assert card.verdict == "no evidence"
    assert not card.table.at["Mean return, Newey-West t", "passes"]


def test_a_strong_steady_edge_is_evidence() -> None:
    card = stc.strategy_card(_returns(2.0, seed=2), name="edge", standard=FAST, ledger=False)
    assert card.verdict == "evidence of an edge", card.table
    low = float(card.table.at["Sharpe (annual), interval", "value"].split("(")[1].split(" to ")[0])
    assert low > 0.5


def test_short_history_gives_no_verdict() -> None:
    card = stc.strategy_card(_returns(2.0, seed=3, days=DAYS[:300]), name="short", standard=FAST, ledger=False)
    assert card.verdict == "short history"


def test_the_frozen_holdout_is_cut_off(monkeypatch) -> None:
    monkeypatch.delenv("CQM_FINAL_HOLDOUT", raising=False)
    days = pd.bdate_range("2018-01-01", "2026-06-30")
    card = stc.strategy_card(_returns(1.0, seed=4, days=days), name="cut", standard=FAST, ledger=False)
    assert card.details["span"].split(" to ")[1].startswith("2025-12")


def test_romano_wolf_is_at_least_the_raw_p_and_keeps_a_real_edge_among_noise() -> None:
    variants = {f"noise_{index}": _returns(0.0, seed=10 + index) for index in range(15)}
    card = stc.strategy_card(_returns(1.5, seed=5), name="real", variants=variants, standard=FAST, ledger=False)
    table = card.variants
    assert (table["p vs 0, Romano-Wolf"] >= table["p vs 0"] - 1e-12).all()
    assert table.at["real", "p vs 0, Romano-Wolf"] < 0.05
    assert table.drop(index="real")["p vs 0, Romano-Wolf"].min() > 0.05


def test_pbo_is_about_a_half_for_noise_and_low_when_one_configuration_is_really_better() -> None:
    noise = pd.DataFrame({f"c{index}": _returns(0.0, seed=100 + index) for index in range(12)})
    assert 0.25 < stc.pbo_cscv(noise, blocks=10)["pbo"] < 0.75
    better = noise.assign(c0=_returns(2.0, seed=200))
    assert stc.pbo_cscv(better, blocks=10)["pbo"] < 0.2


def test_shrinkage_pulls_toward_the_prior_more_when_the_evidence_is_weak() -> None:
    weak = stc.shrink(1.0, 1.0, 0.0, 0.5)
    strong = stc.shrink(1.0, 0.1, 0.0, 0.5)
    assert 0.0 < weak[0] < strong[0] < 1.0
    assert weak[1] > strong[1]


def test_the_timing_placebo_tells_timing_from_exposure() -> None:
    rng = np.random.default_rng(6)
    market = pd.Series(rng.normal(0.0003, 0.01, len(DAYS)), index=DAYS)
    informed = pd.Series(np.where(rng.random(len(DAYS)) < 0.55, np.sign(market), -np.sign(market)), index=DAYS)
    random = pd.Series(np.sign(rng.normal(size=len(DAYS))), index=DAYS)
    assert stc.timing_placebo(informed, market, runs=200, min_shift=60, periods_per_year=252, seed=0)["p"] < 0.05
    assert stc.timing_placebo(random, market, runs=200, min_shift=60, periods_per_year=252, seed=0)["p"] > 0.05


def test_a_strategy_ahead_of_its_baseline_every_day_is_better() -> None:
    base = _returns(0.5, seed=7)
    card = stc.strategy_card(base + 0.0004, name="ahead", baseline=base, standard=FAST, ledger=False)
    assert card.versus_baseline == "better"
    same = stc.strategy_card(base, name="same", baseline=_returns(0.5, seed=8), standard=FAST, ledger=False)
    assert same.versus_baseline == "not shown better"


def test_the_card_saves_a_page(tmp_path) -> None:
    card = stc.strategy_card(_returns(1.0, seed=9), name="page", variants={"other": _returns(0.5, seed=11)}, standard=FAST, ledger=False)
    out = card.save(tmp_path / "card")
    assert (out / "card.md").read_text().startswith("# Strategy card: page")
    assert (out / "card.png").exists()


def test_the_strategy_listed_again_among_its_variants_counts_once() -> None:
    returns, other = _returns(1.0, seed=12), _returns(0.5, seed=13)
    card = stc.strategy_card(returns, name="renamed", variants={"P": returns, "B": other}, standard=FAST, ledger=False)
    assert list(card.variants.index) == ["renamed", "B"]


def _bar_loader(instrument, interval):
    """A synthetic daily random walk for any instrument (no network, no cache)."""
    from datetime import datetime, timedelta, timezone

    from src.storage.bar_aggregator import OHLCVBar

    rng = np.random.default_rng(abs(hash(instrument.id)) % 1000)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.03, 1800)))
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    return [OHLCVBar(exchange=instrument.venue, symbol=instrument.symbol, interval_seconds=86_400, timestamp=start + timedelta(days=index),
                     open=float(close), high=float(close) * 1.01, low=float(close) * 0.99, close=float(close), volume=1.0)
            for index, close in enumerate(closes)]


def test_card_sleeve_runs_a_book_sleeve_with_its_variants_baseline_and_placebo() -> None:
    card = stc.card_sleeve("config/portfolio.multi_paper.toml", "btc_ma_1d", variants={"fast": {"short_window": 2}}, bar_loader=_bar_loader,
                           standard=FAST, ledger=False)
    assert card.verdict in stc.VERDICTS
    assert list(card.variants.index)[1:] == ["fast"]
    assert card.versus_baseline in ("better", "not shown better")
    assert isinstance(card.table.at["Timing placebo p", "passes"], bool)
    assert card.details["subject"].startswith("sleeve|")


def test_card_sleeve_takes_a_candidate_and_refuses_an_unknown_instrument() -> None:
    from src.portfolio.sleeves import SleeveSpec

    candidate = SleeveSpec(id="new", instrument="kraken_futures:ETH/USD", interval="1d", strategy="moving_average_crossover",
                           params={"short_window": 4, "long_window": 48}, long_only=True, sizing="vol_target", sizing_params={"target_annual_vol": 0.5})
    card = stc.card_sleeve("config/portfolio.multi_paper.toml", candidate, variants=None, bar_loader=_bar_loader, standard=FAST, ledger=False)
    assert card.variants.index.tolist() == [card.name]
    with pytest.raises(KeyError):
        stc.card_sleeve("config/portfolio.multi_paper.toml", "no_such_sleeve", bar_loader=_bar_loader, ledger=False)


def test_the_portfolio_check_gate_reads_the_latest_card_from_the_ledger(tmp_path, monkeypatch, capsys) -> None:
    from types import SimpleNamespace

    from main import strategy_card_status
    from src.portfolio.sleeves import SleeveSpec

    monkeypatch.setenv("CQM_TRIAL_LEDGER", str(tmp_path / "ledger.jsonl"))
    spec = SleeveSpec(id="s", instrument="kraken_futures:BTC/USD", interval="1d", strategy="moving_average_crossover", params={"short_window": 4, "long_window": 48})
    config = SimpleNamespace(enabled_sleeves=[spec], baskets=[])
    assert strategy_card_status(config) == 0 and "no card yet" in capsys.readouterr().out
    assert strategy_card_status(config, require=True) == 1
    stc.strategy_card(_returns(0.0, seed=21), name="s noise", standard=FAST, subject=stc.sleeve_key(spec))
    assert strategy_card_status(config, require=True) == 1  # "no evidence" fails the gate
    stc.strategy_card(_returns(2.0, seed=22), name="s edge", standard=FAST, subject=stc.sleeve_key(spec))
    assert strategy_card_status(config, require=True) == 0  # the latest card counts
    assert stc.latest_cards()[stc.sleeve_key(spec)]["verdict"] == "evidence of an edge"


def test_the_sleeve_key_ignores_id_and_budget_but_not_parameters() -> None:
    from dataclasses import replace

    from src.portfolio.sleeves import SleeveSpec

    spec = SleeveSpec(id="a", instrument="kraken_futures:BTC/USD", interval="1d", strategy="moving_average_crossover", params={"short_window": 4, "long_window": 48})
    assert stc.sleeve_key(spec) == stc.sleeve_key(replace(spec, id="b", budget=0.5))
    assert stc.sleeve_key(spec) != stc.sleeve_key(replace(spec, params={"short_window": 8, "long_window": 48}))
