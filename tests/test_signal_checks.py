"""The signal card (src/research/signal_checks.py): each check on synthetic data whose answer is known."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.research import governance
from src.research import signal_checks as sc

N = 6000
INDEX = pd.date_range("2021-01-01", periods=N, freq="h")
STANDARD = sc.Standard()


def market(seed: int, n: int = N) -> tuple[pd.Series, pd.Series]:
    """(one-bar log returns, close) of a random walk."""
    returns = pd.Series(np.random.default_rng(seed).normal(0.0, 0.01, n), index=INDEX[:n])
    return returns, np.exp(returns.cumsum())


def slow_noise(seed: int, span: int = 50, n: int = N) -> pd.Series:
    """A persistent series with no link to any return."""
    return pd.Series(np.random.default_rng(seed).normal(size=n), index=INDEX[:n]).ewm(span=span).mean()


def planted(returns: pd.Series, strength: float, seed: int, horizon: int = 5) -> pd.Series:
    """A persistent signal that knows a little about the next `horizon` bars' return, as of each bar's close."""
    future = returns.shift(-1).rolling(horizon).sum().shift(-(horizon - 1)) / (0.01 * np.sqrt(horizon))
    return (strength * future + slow_noise(seed, span=10)).rename("planted")


def test_standard_file_loads_and_rejects_a_typo(tmp_path) -> None:
    assert sc.load_standard() == STANDARD  # the committed file is version 1, equal to the defaults in code
    (tmp_path / "bad.toml").write_text("version = 2\nplacebo = 0.5\n")
    with pytest.raises(ValueError, match="unknown keys"):
        sc.load_standard(tmp_path / "bad.toml")


def test_ic_and_forward_returns() -> None:
    returns, close = market(1)
    forward = sc.forward_returns(close, 5)
    assert forward.iloc[10] == pytest.approx(returns.iloc[11:16].sum()) and forward.iloc[-5:].isna().all()
    assert sc.ic(forward, forward) == pytest.approx(1.0) and sc.ic(-forward, forward) == pytest.approx(-1.0)
    assert np.isnan(sc.ic(pd.Series(1.0, index=INDEX), forward))  # a constant has no ranking


def test_effective_n_falls_with_persistence_and_overlap() -> None:
    _returns, close = market(2)
    fast = pd.Series(np.random.default_rng(3).normal(size=N), index=INDEX)
    assert sc.effective_n(fast, sc.forward_returns(close, 1), 1) > 0.8 * N
    slow_long = sc.effective_n(slow_noise(4, span=200), sc.forward_returns(close, 24), 24)
    assert slow_long < N / 24  # fewer than bars / horizon: both sides are persistent
    low, high = sc.ic_interval(0.10, 100)
    assert low < 0.10 < high and low == pytest.approx(np.tanh(np.arctanh(0.10) - 1.96 / np.sqrt(97)), abs=1e-4)
    assert sc.ic_interval(0.10, 10_000)[0] > 0.08 and np.isnan(sc.ic_interval(0.1, 3)[0])


def test_newey_west_widens_the_error_of_overlapping_sums() -> None:
    returns, _close = market(5)
    overlapping = returns.rolling(20).sum().dropna()
    _mean, plain = sc.newey_west_mean(overlapping, 0)
    _mean, robust = sc.newey_west_mean(overlapping, 19)
    assert robust > 3 * plain
    assert sc.newey_west_mean([1.0, 2.0], 1) != sc.newey_west_mean([1.0, 2.0], 1)  # too short: NaN


def test_expanding_buckets_never_use_later_values() -> None:
    signal = slow_noise(6)
    full = sc.expanding_buckets(signal, 5, 100)
    early = sc.expanding_buckets(signal.iloc[:3000], 5, 100)
    pd.testing.assert_series_equal(full.iloc[:3000], early)
    assert full.iloc[:99].isna().all() and set(full.dropna().unique()) == {1.0, 2.0, 3.0, 4.0, 5.0}


def test_shift_placebo_matches_a_direct_shift_and_is_calibrated_on_noise() -> None:
    _returns, close = market(7)
    forward = sc.forward_returns(close, 5)
    signal = slow_noise(8)
    frame = pd.DataFrame({"s": signal, "f": forward}).dropna()
    direct = np.corrcoef(pd.Series(np.roll(frame["s"].to_numpy(), 137)).rank(), frame["f"].rank().to_numpy())[0, 1]
    a, b = sc._centred_ranks(frame["s"]), sc._centred_ranks(frame["f"])
    every = np.fft.irfft(np.fft.rfft(b) * np.conj(np.fft.rfft(a)), len(frame)) / np.sqrt((a @ a) * (b @ b))
    assert every[137] == pytest.approx(direct, abs=1e-9) and every[0] == pytest.approx(sc.ic(signal, forward), abs=1e-9)
    # No link between signal and return: the p-value is uniform, so about 5% fall below 0.05
    below = [sc.shift_placebo(slow_noise(100 + seed), forward, 5)["p"] < 0.05 for seed in range(120)]
    assert 0.0 <= np.mean(below) <= 0.12


def test_interval_is_calibrated_on_persistent_noise() -> None:
    """A slow signal against overlapping returns: with the effective sample size, about 5% of intervals miss zero."""
    _returns, close = market(9)
    forward = sc.forward_returns(close, 24)
    misses = []
    for seed in range(120):
        signal = slow_noise(300 + seed, span=100)
        low, high = sc.ic_interval(sc.ic(signal, forward), sc.effective_n(signal, forward, 24))
        misses.append(low > 0 or high < 0)
    assert np.mean(misses) <= 0.12
    naive = [abs(sc.ic(slow_noise(300 + seed, span=100), forward)) * np.sqrt(N) > 1.96 for seed in range(120)]
    assert np.mean(naive) > 0.5  # counting every bar as independent calls most of this noise significant


def test_card_finds_a_planted_signal_and_clears_every_check() -> None:
    returns, close = market(10)
    card = sc.signal_card(planted(returns, 0.35, 11), close, horizons=[5, 20], name="planted", direction=1, round_trip_bps=2.0, ledger=False)
    assert card.verdict == "worth a strategy test", card.reason
    assert card.table.loc["IC", "passes"] and card.table.loc["placebo p", "passes"] and card.table.loc["best bucket bps", "passes"]
    assert list(card.by_horizon.columns) == [5, 20] and card.by_horizon.at["IC", 5] > card.by_horizon.at["IC", 20] > 0
    assert card.primary == 5 and "pass" in card.to_markdown() and "FAIL" not in card.to_markdown()


def test_card_says_no_evidence_for_noise_and_for_the_wrong_direction() -> None:
    """The card is a screen at about the 5% level: nearly all noise is turned away, a few lucky draws are not.
    That is why every card costs a trial and `compare` gives the luck threshold for the best of many."""
    verdicts = [sc.signal_card(slow_noise(5000 + seed), market(1000 + seed)[1], horizons=[5], ledger=False).verdict for seed in range(100)]
    assert verdicts.count("no evidence") >= 88 and verdicts.count("worth a strategy test") <= 6
    returns, close = market(12)
    wrong = sc.signal_card(planted(returns, 0.35, 14), close, horizons=[5], direction=-1, round_trip_bps=2.0, ledger=False)
    assert wrong.verdict == "no evidence" and "opposite sign" in wrong.reason


def test_a_signal_that_needs_its_own_bar_fails_the_delay_check() -> None:
    """The return of the bar right after the close, known one bar too early: a large IC that is gone one bar later."""
    returns, close = market(15)
    leaky = (returns.shift(-1) / 0.01 + pd.Series(np.random.default_rng(16).normal(size=N), index=INDEX)).rename("leaky")
    card = sc.signal_card(leaky, close, horizons=[1], round_trip_bps=2.0, ledger=False)
    assert card.by_horizon.at["IC", 1] > 0.5 and not card.table.loc["IC kept 1 bar late", "passes"]
    assert card.verdict == "weak" and "IC kept 1 bar late" in card.reason


def test_a_real_effect_too_small_for_the_cost_is_weak() -> None:
    returns, close = market(17)
    card = sc.signal_card(planted(returns, 0.35, 18), close, horizons=[5], direction=1, round_trip_bps=500.0, ledger=False)
    assert card.verdict == "weak" and "best bucket bps" in card.reason


def test_an_effect_in_one_year_only_fails_stability_and_concentration() -> None:
    n = 4 * 365
    index = pd.date_range("2021-01-01", periods=n, freq="D")
    rng = np.random.default_rng(19)
    returns = pd.Series(rng.normal(0.0, 0.02, n), index=index)
    knows = pd.Series(np.where(index.year == 2022, 1.5, -0.15), index=index)  # strong in 2022, slightly the other way otherwise
    signal = knows * returns.shift(-1) / 0.02 + pd.Series(rng.normal(size=n), index=index)
    card = sc.signal_card(signal, np.exp(returns.cumsum()), horizons=[1], round_trip_bps=1.0, ledger=False)
    assert float(card.table.loc["IC", "value"]) > 0 and card.table.loc["blocks", "value"] == "4"
    assert not card.table.loc["blocks with the same sign", "passes"] and not card.table.loc["IC without the best block", "passes"]
    assert card.verdict in ("weak", "no evidence") and "worth" not in card.verdict


def test_short_history_gives_no_verdict() -> None:
    _returns, close = market(20, n=400)
    card = sc.signal_card(slow_noise(21, span=100, n=400), close, horizons=[48], ledger=False)
    assert card.verdict == "short history" and "independent observations" in card.reason


def test_forward_returns_can_be_given_directly() -> None:
    days = pd.date_range("2022-01-03", periods=600, freq="B")
    rng = np.random.default_rng(22)
    morning = pd.Series(rng.normal(0.0, 0.01, 600), index=days)
    afternoon = 0.4 * morning + pd.Series(rng.normal(0.0, 0.01, 600), index=days)
    card = sc.signal_card(morning, forward=afternoon, name="morning", direction=1, round_trip_bps=5.0, ledger=False)
    assert card.table.loc["IC", "passes"] and card.table.loc["baseline IC", "passes"] is None and card.table.loc["IC kept 1 bar late", "passes"] is None
    assert card.verdict == "worth a strategy test" and "not applicable here: IC kept 1 bar late, baseline IC" in card.reason  # no prices: two checks cannot apply
    with pytest.raises(ValueError, match="one of them"):
        sc.signal_card(morning, ledger=False)
    with pytest.raises(ValueError, match="primary"):
        sc.signal_card(morning, forward=afternoon, horizons=[1], primary=5, ledger=False)


# --- a ranking of markets ---------------------------------------------------------------------------------------------

def panel(seed: int, strength: float, days: int = 900, markets: int = 12) -> tuple[pd.DataFrame, pd.DataFrame]:
    index = pd.date_range("2021-01-01", periods=days, freq="D")
    rng = np.random.default_rng(seed)
    common = rng.normal(0.0, 0.02, (days, 1))
    returns = pd.DataFrame(common + rng.normal(0.0, 0.02, (days, markets)), index=index, columns=[f"m{i}" for i in range(markets)])
    own = returns.sub(returns.mean(axis=1), axis=0)
    ahead = own.shift(-1).rolling(3).sum().shift(-2) / (0.02 * np.sqrt(3))  # what each market does against the others over the next three days
    signal = strength * ahead + pd.DataFrame(rng.normal(size=(days, markets)), index=index, columns=returns.columns)
    return signal, np.exp(returns.cumsum())


def test_ic_by_date_and_top_minus_rest() -> None:
    index = pd.date_range("2024-01-01", periods=2, freq="D")
    signal = pd.DataFrame([[1, 2, 3, 4, 5], [1, 2, 3, np.nan, np.nan]], index=index, columns=list("abcde"), dtype=float)
    forward = pd.DataFrame([[0.01, 0.02, 0.03, 0.04, 0.05], [0.03, 0.02, 0.01, 0.0, 0.0]], index=index, columns=list("abcde"))
    by_date = sc.ic_by_date(signal, forward, min_markets=5)
    assert by_date.iloc[0] == pytest.approx(1.0) and np.isnan(by_date.iloc[1])  # only three markets on the second date
    assert sc.ic_by_date(signal, forward, min_markets=3).iloc[1] == pytest.approx(-1.0)
    groups = sc.top_minus_rest(signal, forward, buckets=5, min_markets=5)
    assert groups["top"].iloc[0] == pytest.approx(0.02) and groups["bottom"].iloc[0] == pytest.approx(-0.02)


def test_panel_card_finds_a_planted_ranking_and_rejects_noise() -> None:
    signal, close = panel(23, strength=0.35)
    card = sc.signal_card(signal, close, horizons=[3], name="ranking", direction=1, round_trip_bps=2.0, ledger=False)
    assert card.kind == "ranking of markets" and card.verdict == "worth a strategy test", card.reason
    assert card.table.loc["IC without the best market", "passes"] and set(card.details["per_market"].index) == set(signal.columns)
    noise, close = panel(24, strength=0.0)
    assert sc.signal_card(noise, close, horizons=[3], ledger=False).verdict == "no evidence"


# --- ledger, holdout, gate, output ------------------------------------------------------------------------------------

def test_card_is_logged_once_and_the_gate_reads_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CQM_TRIAL_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("CQM_PREREG_DIR", str(tmp_path))
    returns, close = market(25)
    with pytest.raises(sc.CardGate, match="run signal_checks.signal_card"):
        sc.require_card("planted")
    good = planted(returns, 0.35, 26)
    sc.signal_card(good, close, horizons=[5, 20], name="planted", direction=1, round_trip_bps=2.0, family="H99")
    sc.signal_card(good, close, horizons=[5, 20], name="planted", direction=1, round_trip_bps=2.0, family="H99")  # the same again: not counted twice
    entries = governance.ledger_entries()
    assert len(entries) == 1 and entries[0]["configurations"] == 2 and entries[0]["family"] == "H99" and entries[0]["details"]["verdict"] == "worth a strategy test"
    assert sc.require_card("planted")["primary"] == 5

    sc.signal_card(slow_noise(5001), close, horizons=[5], name="nothing")
    with pytest.raises(sc.CardGate, match="no evidence"):
        sc.require_card("nothing", "H98")
    (tmp_path / "H98.txt").write_text("PRE-REGISTRATION H98\nSIGNAL CARD OVERRIDE:\n")
    with pytest.raises(sc.CardGate):
        sc.require_card("nothing", "H98")  # an override needs a reason
    (tmp_path / "H98.txt").write_text("PRE-REGISTRATION H98\nSIGNAL CARD OVERRIDE: the effect is only expected after 2024, tested as a strategy with that filter\n")
    assert "only expected after 2024" in sc.require_card("nothing", "H98")["override"]
    assert governance.ledger_entries()[-1]["study"] == "signal_card_override:nothing" and sum(entry.get("configurations", 0) for entry in governance.ledger_entries()) == 3  # the override is recorded, not counted as a trial


def test_the_frozen_holdout_is_cut_off(monkeypatch) -> None:
    monkeypatch.delenv("CQM_FINAL_HOLDOUT", raising=False)
    index = pd.date_range("2024-01-01", "2026-06-30", freq="D")
    rng = np.random.default_rng(28)
    returns = pd.Series(rng.normal(0.0, 0.02, len(index)), index=index)
    card = sc.signal_card(pd.Series(rng.normal(size=len(index)), index=index), np.exp(returns.cumsum()), horizons=[1], ledger=False)
    assert int(card.table.loc["bars", "value"]) == len(index[index < "2026-01-01"]) - 1  # nothing from 2026 on; the last bar has no forward return
    with pytest.raises(TypeError, match="DatetimeIndex"):
        sc.signal_card(pd.Series([1.0, 2.0, 3.0]), forward=pd.Series([1.0, 2.0, 3.0]), ledger=False)


def test_card_saves_a_page_tables_and_a_chart(tmp_path) -> None:
    returns, close = market(29)
    card = sc.signal_card(planted(returns, 0.35, 30), close, horizons=[5, 20], name="Planted signal!", round_trip_bps=2.0, ledger=False)
    out = card.save(tmp_path / "card")
    assert {path.name for path in out.iterdir()} == {"card.md", "table.csv", "by_horizon.csv", "card.png"}
    assert (out / "card.md").read_text().startswith("# Signal card: Planted signal!") and (out / "card.png").stat().st_size > 10_000
    signal, panel_close = panel(31, strength=0.25, days=400)
    assert sc.signal_card(signal, panel_close, horizons=[1], ledger=False).save(tmp_path / "panel").joinpath("card.png").exists()


# --- rerunning the builder, and many signals -----------------------------------------------------------------------

def momentum(close: pd.Series | pd.DataFrame) -> pd.Series | pd.DataFrame:
    return np.log(close).diff(20)


def test_pipeline_checks_pass_a_clean_builder() -> None:
    _returns, close = market(40)
    result = sc.pipeline_checks(momentum, close, horizon=5, runs=12)
    assert result.ok and not result.leak and not result.hindsight and abs(result.noise["mean"]) < 0.03 and "clean" in repr(result)
    assert result.table.loc["IC on scrambled returns, mean", "passes"] and (result.replay["largest difference"] == 0).all()


def test_pipeline_checks_catch_a_leak_of_the_next_return() -> None:
    _returns, close = market(41)
    result = sc.pipeline_checks(lambda prices: np.log(prices).diff().shift(-1) + np.log(prices).diff(20) * 0.01, close, horizon=1, runs=12)
    assert result.leak and result.noise["mean"] > 0.5  # it "predicts" returns that were put in a random order
    assert result.hindsight and (result.replay["values only in one run"] > 0).all()  # and each replay lacks the last value the full run has


def test_pipeline_checks_catch_a_whole_sample_average() -> None:
    _returns, close = market(42)

    def standardised(prices: pd.Series) -> pd.Series:
        change = np.log(prices).diff(20)
        return (change - change.mean()) / change.std()  # the mean and the spread of the whole sample: hindsight

    result = sc.pipeline_checks(standardised, close, horizon=5, runs=8)
    assert result.hindsight and not result.leak and (result.replay["largest difference"] > 1e-6).any()


def test_pipeline_checks_work_on_a_panel() -> None:
    _signal, close = panel(43, strength=0.0, days=500)
    result = sc.pipeline_checks(momentum, close, horizon=3, runs=6)
    assert result.ok and np.isfinite(result.z)
    mixed = sc.shuffled_prices(close, np.random.default_rng(0))
    assert mixed.shape == close.shape and np.allclose(mixed.iloc[0], close.iloc[0])
    assert np.allclose(np.sort(np.log(mixed).diff().iloc[1:, 0]), np.sort(np.log(close).diff().iloc[1:, 0]))  # the same returns, another order


def test_compare_groups_copies_and_sets_a_luck_threshold() -> None:
    returns, close = market(44)
    good = planted(returns, 0.35, 45)
    signals = {"good": good, "good_again": good * 2.0 + 0.001 * slow_noise(46), **{f"noise_{i}": slow_noise(600 + i) for i in range(8)}}
    board = sc.compare(signals, close, horizon=5, round_trip_bps=2.0, ledger=False)
    assert board.redundant == [["good", "good_again"]]
    assert set(board.table.index[:2]) == {"good", "good_again"} and board.table.loc["good", "above the luck threshold"]
    assert not board.table.loc[[f"noise_{i}" for i in range(8)], "above the luck threshold"].any()
    alone = sc.shift_placebo(signals["noise_0"], sc.forward_returns(close, 5), 5)["placebo_95"]
    assert board.luck_threshold > alone  # the best of ten needs more than one alone
    assert board.cards["good"].verdict == "worth a strategy test" and len(board.cards) == 10


def test_compare_on_a_panel() -> None:
    signal, close = panel(47, strength=0.35, days=500)
    noise, _close = panel(48, strength=0.0, days=500)
    board = sc.compare({"knows": signal, "noise": noise}, close, horizon=3, round_trip_bps=2.0, ledger=False)
    assert board.table.index[0] == "knows" and board.table.loc["knows", "above the luck threshold"] and not board.table.loc["noise", "above the luck threshold"]
    assert board.redundant == []


def test_a_fixed_tilt_between_markets_is_named_as_such() -> None:
    """Some markets always score high and happened to rise more: a ranking with no timing in it."""
    days, markets = 900, 12
    index = pd.date_range("2021-01-01", periods=days, freq="D")
    rng = np.random.default_rng(50)
    drift = np.linspace(-0.004, 0.004, markets)
    returns = pd.DataFrame(drift + rng.normal(0.0, 0.02, (days, markets)), index=index, columns=[f"m{i}" for i in range(markets)])
    signal = pd.DataFrame(np.arange(markets) + rng.normal(0.0, 0.5, (days, markets)), index=index, columns=returns.columns)
    card = sc.signal_card(signal, np.exp(returns.cumsum()), horizons=[5], round_trip_bps=2.0, ledger=False)
    assert card.table.loc["IC", "passes"] and not card.table.loc["placebo p", "passes"]  # dates say yes, the placebo says the date does not matter
    assert card.verdict == "no evidence" and "fixed tilt" in card.reason
    assert abs(float(card.table.loc["IC of the signal against each market's own history", "value"])) < 0.03
