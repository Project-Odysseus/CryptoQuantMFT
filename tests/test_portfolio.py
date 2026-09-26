import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from src.data import binance_archive
from src.data.binance_archive import base_asset, parse_funding, parse_klines, update_cache, load_panel
from src.research.portfolio import (
    PortfolioCosts,
    cross_sectional_ic,
    liquid_universe,
    rank_weights,
    simulate_portfolio,
    slippage_by_liquidity,
)

DAYS = pd.date_range("2024-01-01", periods=6, freq="D", tz="UTC")
FREE = PortfolioCosts(fee_pct=0.0, slippage_bps=0.0)


def test_a_full_long_position_tracks_the_price_and_pays_costs_on_traded_notional() -> None:
    prices = pd.DataFrame({"A": [100.0, 110.0, 99.0, 99.0, 108.9, 108.9]}, index=DAYS)
    weights = pd.DataFrame({"A": 1.0}, index=DAYS)

    free = simulate_portfolio(prices, weights, costs=FREE)
    assert free.equity.to_numpy() == pytest.approx(prices["A"].to_numpy() / 100.0)

    costly = simulate_portfolio(prices, weights, costs=PortfolioCosts(fee_pct=0.05, slippage_bps=5.0))
    assert costly.costs.iloc[0] == pytest.approx(0.001 / 1.001)  # 1x of the equity left after paying 0.1% all-in
    assert costly.gross_exposure.iloc[0] == pytest.approx(1.0)
    assert costly.turnover.iloc[1] == pytest.approx(0.0, abs=1e-9)  # a price move alone needs no trade at 1x
    assert costly.equity.iloc[-1] < free.equity.iloc[-1]


def test_funding_is_paid_by_longs_and_received_by_shorts() -> None:
    prices = pd.DataFrame({"A": 100.0, "B": 100.0}, index=DAYS)
    funding = pd.DataFrame({"A": 0.001, "B": 0.001}, index=DAYS)
    weights = pd.DataFrame({"A": 0.5, "B": -0.5}, index=DAYS)
    result = simulate_portfolio(prices, weights, funding=funding, costs=FREE)
    assert result.funding.iloc[1:].to_numpy() == pytest.approx(0.0)  # the long pays what the short receives

    long_only = simulate_portfolio(prices, weights.clip(lower=0.0) * 2, funding=funding, costs=FREE)
    assert long_only.equity.iloc[-1] == pytest.approx(0.999**5)


def test_long_short_legs_and_delisting_close_at_the_last_price() -> None:
    prices = pd.DataFrame({"A": [100.0, 110.0, 110.0, 110.0, 110.0, 110.0], "B": [100.0, 90.0, 45.0, np.nan, np.nan, np.nan]}, index=DAYS)
    weights = pd.DataFrame({"A": 0.5, "B": -0.5}, index=DAYS)
    result = simulate_portfolio(prices, weights, costs=FREE)

    assert result.long_pnl.iloc[1] == pytest.approx(0.05) and result.short_pnl.iloc[1] == pytest.approx(0.05)
    assert result.returns.iloc[1] == pytest.approx(0.10)
    # B halves on day 2, then stops trading: the short is closed at 45 and never reopened
    assert result.positions.iloc[3] == 1.0 and result.net_exposure.iloc[3] == pytest.approx(0.5)
    assert result.equity.iloc[3] == pytest.approx(result.equity.iloc[2])


def test_positions_drift_between_rebalances_without_costs() -> None:
    prices = pd.DataFrame({"A": [100.0, 200.0, 200.0, 100.0, 100.0, 100.0], "B": 100.0}, index=DAYS)
    weights = pd.DataFrame({"A": 0.5, "B": 0.5}, index=DAYS)
    result = simulate_portfolio(prices, weights, costs=PortfolioCosts(fee_pct=0.1, slippage_bps=0.0), rebalance_every=2)
    assert result.costs.iloc[1] == 0.0 and result.costs.iloc[2] > 0.0  # day 1 drifts, day 2 rebalances
    assert result.net_exposure.iloc[1] == pytest.approx(1.0) and result.gross_exposure.iloc[1] == pytest.approx(1.0)


def test_rank_weights_are_dollar_neutral_quantile_legs() -> None:
    coins = [f"C{i}" for i in range(10)]
    scores = pd.DataFrame([list(range(10))], columns=coins, index=DAYS[:1])
    eligible = pd.DataFrame(True, index=scores.index, columns=coins)

    weights = rank_weights(scores, eligible, quantile=0.2, gross=1.0)
    assert weights.iloc[0].to_dict() == {**{c: 0.0 for c in coins}, "C8": 0.25, "C9": 0.25, "C0": -0.25, "C1": -0.25}
    assert rank_weights(scores, eligible, long_only=True).iloc[0][["C8", "C9"]].tolist() == [0.5, 0.5]
    assert (rank_weights(scores, eligible, min_names=11).iloc[0] == 0.0).all()

    risk = pd.DataFrame([[1.0] * 8 + [1.0, 3.0]], columns=coins, index=scores.index)
    inverse_vol = rank_weights(scores, eligible, risk=risk).iloc[0]
    assert inverse_vol["C8"] == pytest.approx(0.375) and inverse_vol["C9"] == pytest.approx(0.125)


def test_liquid_universe_uses_only_past_volume_and_listing_age() -> None:
    days = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")
    volume = pd.DataFrame({"A": [1, 1, 1, 1, 1], "B": [np.nan, np.nan, 5, 5, 5], "C": [3, 3, 3, 3, 3]}, index=days, dtype=float)
    universe = liquid_universe(volume, top_n=1, lookback_days=2, min_history_days=2)
    assert universe.loc[days[1]].to_dict() == {"A": False, "B": False, "C": True}
    assert universe.loc[days[3]].to_dict() == {"A": False, "B": True, "C": False}  # B qualifies after two days listed


def test_cross_sectional_ic_and_slippage_tiers() -> None:
    coins = [f"C{i}" for i in range(12)]
    scores = pd.DataFrame([np.arange(12.0)], columns=coins, index=DAYS[:1])
    eligible = pd.DataFrame(True, index=scores.index, columns=coins)
    assert cross_sectional_ic(scores, scores * 2, eligible).iloc[0] == pytest.approx(1.0)
    assert cross_sectional_ic(scores, -scores, eligible).iloc[0] == pytest.approx(-1.0)

    volume = pd.DataFrame({"big": [2e9], "mid": [3e8], "small": [6e7], "tiny": [1e6]}, index=DAYS[:1])
    assert slippage_by_liquidity(volume).iloc[0].to_dict() == {"big": 3.0, "mid": 8.0, "small": 15.0, "tiny": 25.0}


def _zip_csv(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("file.csv", text)
    return buffer.getvalue()


def test_archive_parsing_handles_headers_microseconds_and_funding_days() -> None:
    headerless = "1704067200000,100,110,90,105,10,1704153599999,1050,7,4,420,0\n"
    with_header = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n1735689600000000,1,2,0.5,1.5,3,1735775999999999,4.5,2,1,1.5,0\n"
    first = parse_klines(binance_archive._read_zip_csv(_zip_csv(headerless), binance_archive.KLINE_COLUMNS))
    second = parse_klines(binance_archive._read_zip_csv(_zip_csv(with_header), binance_archive.KLINE_COLUMNS))
    assert f"{first['date'].iloc[0]:%Y-%m-%d}" == "2024-01-01" and first["close"].iloc[0] == 105.0 and first["trades"].iloc[0] == 7
    assert f"{second['date'].iloc[0]:%Y-%m-%d}" == "2025-01-01" and second["taker_buy_quote_volume"].iloc[0] == 1.5

    funding = pd.DataFrame({"calc_time": [1704096000000, 1704124800000, 1704153600000], "funding_interval_hours": 8, "last_funding_rate": [0.0001, 0.0002, 0.0003]})
    daily = parse_funding(funding)  # 08:00, 16:00 and 24:00 on 2024-01-01 all belong to that day's holding
    assert len(daily) == 1 and daily["funding"].iloc[0] == pytest.approx(0.0006)
    assert base_asset("1000PEPEUSDT") == "PEPE" and base_asset("1INCHUSDT") == "1INCH" and base_asset("1000000MOGUSDT") == "MOG"


def test_update_cache_fetches_only_missing_months(tmp_path, monkeypatch) -> None:
    keys = [f"data/futures/um/monthly/klines/AUSDT/1d/AUSDT-1d-2024-0{m}.zip" for m in (1, 2)]
    fetched: list[str] = []

    def fake_list(prefix: str, *, tag: str) -> list[str]:
        return keys if "klines" in prefix else []

    def fake_fetch(url: str, *, retries: int = 4) -> bytes:
        fetched.append(url)
        month = int(url[-6:-4])
        stamp = int(pd.Timestamp(f"2024-0{month}-01", tz="UTC").timestamp() * 1000)
        return _zip_csv(f"{stamp},1,1,1,1,1,{stamp + 86399999},1,1,1,1,0\n")

    monkeypatch.setattr(binance_archive, "_list", fake_list)
    monkeypatch.setattr(binance_archive, "_fetch", fake_fetch)
    assert update_cache(["AUSDT"], datasets=("klines_1d",), cache_dir=tmp_path) == {"klines_1d": 2}
    keys.append("data/futures/um/monthly/klines/AUSDT/1d/AUSDT-1d-2024-03.zip")
    assert update_cache(["AUSDT"], datasets=("klines_1d",), cache_dir=tmp_path) == {"klines_1d": 1}
    panel = load_panel("klines_1d", cache_dir=tmp_path)
    assert panel["symbol"].unique().tolist() == ["AUSDT"] and len(panel) == 3


def test_cross_sectional_study_runs_end_to_end_on_synthetic_data(tmp_path, monkeypatch, capsys) -> None:
    import runpy
    import sys

    rng = np.random.default_rng(1)
    days = pd.date_range("2020-01-01", "2024-06-30", freq="D", tz="UTC")
    market = np.cumsum(rng.normal(0, 0.03, len(days)))
    for number in range(16):
        symbol = "BTCUSDT" if number == 0 else f"C{number}USDT"
        drift = np.cumsum(rng.normal(0, 0.04, len(days)))
        close = 100 * np.exp(market + drift)
        frame = pd.DataFrame({"date": days, "open": close, "high": close, "low": close, "close": close,
                              "volume": 1.0, "quote_volume": rng.uniform(1e7, 1e9, len(days)), "trades": 1.0,
                              "taker_buy_quote_volume": rng.uniform(4e6, 5e8, len(days))})
        if number == 15:
            frame = frame.iloc[:900]  # delisted part-way through
        (tmp_path / "klines_1d").mkdir(exist_ok=True)
        (tmp_path / "funding").mkdir(exist_ok=True)
        frame.to_parquet(tmp_path / "klines_1d" / f"{symbol}.parquet", index=False)
        pd.DataFrame({"date": days, "funding": rng.normal(1e-4, 1e-4, len(days))}).to_parquet(tmp_path / "funding" / f"{symbol}.parquet", index=False)

    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["cross_sectional_study.py", "--cache-dir", str(tmp_path), "--top-n", "12", "--out", str(out)])
    runpy.run_path("scripts/research/cross_sectional_study.py", run_name="__main__")

    ic = pd.read_csv(out / "ic.csv")
    portfolios = pd.read_csv(out / "portfolios.csv")
    assert len(ic) == 8 and {"is_ic_7d", "ho_t_7d", "years_same_sign_7d"} <= set(ic.columns)
    assert set(portfolios["period"]) == {"is", "ho"} and set(portfolios["rebalance_days"]) == {1, 7}
    assert portfolios["sharpe"].notna().all() and (portfolios["avg_gross_exposure"] > 0.5).all()
    assert "Long/short portfolios" in capsys.readouterr().out
