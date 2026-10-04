"""Kraken Futures spread and depth recording (src/data/kraken_spreads.py). No network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.data import kraken_spreads as ks

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
TICKERS = {"tickers": [
    {"symbol": "PF_XBTUSD", "bid": 84_742, "ask": 84_743, "bidSize": 0.28, "askSize": 0.02, "markPrice": 84_745.5, "volumeQuote": 146_665_832.7},
    {"symbol": "PF_QNTUSD", "bid": 99.73, "ask": 100.0, "bidSize": 5, "askSize": 6, "markPrice": 99.9, "volumeQuote": 5_400_000},
    {"symbol": "FF_XBTUSD_261225", "bid": 85_000, "ask": 85_010},   # a dated future: not a perp
    {"symbol": "PF_DEADUSD", "bid": 0, "ask": 1.0},                  # no bid: left out
]}
BOOK = {"orderBook": {"bids": [[99.0, 5.0], [100.0, 2.0], [99.5, 10.0]], "asks": [[101.0, 2.0], [101.5, 10.0], [102.0, 50.0]]}}  # Kraken's bids come unsorted


def _fetch(url: str):
    if "tickers" in url:
        return TICKERS
    if "symbol=PF_BAD" in url:
        raise TimeoutError("down")
    return BOOK


def test_tickers_give_each_perps_half_spread() -> None:
    frame = ks.fetch_tickers(now=NOW, fetch=_fetch)
    assert frame["symbol"].tolist() == ["PF_XBTUSD", "PF_QNTUSD"]
    assert frame.loc[0, "half_spread_bps"] == pytest.approx(0.5 / 84_742.5 * 1e4) and frame.loc[1, "half_spread_bps"] == pytest.approx(0.135 / 99.865 * 1e4)
    assert frame.loc[0, "volume_quote_24h"] == pytest.approx(146_665_832.7) and frame.loc[0, "time"] == "2026-10-04T12:00:00+00:00"


def test_a_market_orders_cost_walks_the_book_from_the_best_price() -> None:
    asks = [[101.0, 2.0], [101.5, 10.0], [102.0, 50.0]]
    mid = 100.5
    assert ks.market_order_cost_bps(asks, mid, 101.0) == pytest.approx((101.0 / mid - 1) * 1e4)  # filled at the touch
    # 500 USD: 202 at 101.0, the remaining 298 at 101.5
    assert ks.market_order_cost_bps(asks, mid, 500.0) == pytest.approx((202 * (101.0 / mid - 1) + 298 * (101.5 / mid - 1)) / 500 * 1e4)
    assert np.isnan(ks.market_order_cost_bps(asks, mid, 1e9))  # more than the book shows
    depth = ks.fetch_depth("PF_QNTUSD", sizes_usd=(100.0, 500.0), now=NOW, fetch=_fetch)
    assert depth["mid"].iloc[0] == 100.5  # best bid 100.0 found although the bids arrive unsorted
    assert depth.loc[0, "buy_bps"] == pytest.approx((101.0 / 100.5 - 1) * 1e4) and depth.loc[0, "sell_bps"] == pytest.approx((1 - 100.0 / 100.5) * 1e4)
    assert depth.loc[1, "sell_bps"] > depth.loc[0, "sell_bps"]  # 500 USD takes the 100.0 bid (200 USD) and part of 99.5


def test_samples_are_appended_and_summarised_against_the_assumed_slippage(tmp_path) -> None:
    for minutes in (0, 10, 20):
        assert ks.record_tickers(root=tmp_path, now=NOW + timedelta(minutes=minutes), fetch=_fetch) == 2
    assert ks.record_depth(["PF_QNTUSD", "PF_BAD"], root=tmp_path, now=NOW, fetch=_fetch) == 3  # the failing coin is skipped, the other's three sizes are kept
    assert (tmp_path / "tickers" / "2026-10-04.csv").read_text().count("\n") == 1 + 6  # one header, appended to
    assert len(ks.load("tickers", root=tmp_path)) == 6 and len(ks.load("depth", root=tmp_path)) == 3 and ks.load("depth", root=tmp_path / "none").empty
    table = ks.cost_table({"PF_QNTUSD": 10.0, "PF_XBTUSD": 5.0, "PF_NEVERUSD": 5.0}, root=tmp_path, size_usd=1_000.0)
    assert table.index.tolist() == ["PF_XBTUSD", "PF_QNTUSD"]  # by volume; a coin never seen is left out
    qnt = table.loc["PF_QNTUSD"]
    assert qnt["samples"] == 3 and qnt["half_spread_median"] == pytest.approx(13.5, abs=0.1) and qnt["assumed_bps"] == 10.0
    assert qnt["measured_bps"] == pytest.approx(max(qnt["half_spread_median"], qnt["cost_at_1000_usd"])) and qnt["ratio"] == pytest.approx(qnt["measured_bps"] / 10.0) and qnt["ratio"] > 1
    btc = table.loc["PF_XBTUSD"]
    assert np.isnan(btc["cost_at_1000_usd"]) and btc["measured_bps"] == pytest.approx(btc["half_spread_median"]) and btc["ratio"] < 0.1  # no depth sample: the spread alone
    assert ks.cost_table({"PF_XBTUSD": 5.0}, root=tmp_path / "none").empty


def test_touch_quotes_answer_per_runtime_symbol_from_one_cached_request() -> None:
    calls = []
    clock = [0.0]

    def fetch(url: str):
        calls.append(url)
        if len(calls) == 3:
            raise TimeoutError("down")
        return TICKERS

    quotes = ks.TouchQuotes(max_age=15.0, fetch=fetch, clock=lambda: clock[0])
    assert quotes("BTC/USD") == (84_742.0, 84_743.0) and quotes("QNT/USD") == (99.73, 100.0) and quotes("NOPE/USD") is None
    assert len(calls) == 1  # one request answers for every coin
    clock[0] = 20.0
    assert quotes("BTC/USD") == (84_742.0, 84_743.0) and len(calls) == 2  # older than max_age: asked again
    clock[0] = 40.0
    assert quotes("BTC/USD") is None and len(calls) == 3  # Kraken unreachable: "unknown", not an exception and not a stale price


def test_funding_rates_are_krakens_relative_rate_per_day_and_survive_an_outage(tmp_path) -> None:
    tickers = {"tickers": [{"symbol": "PF_XBTUSD", "bid": 84_999, "ask": 85_001, "fundingRate": 0.4025, "markPrice": 85_000.0}, {"symbol": "PF_QNTUSD", "bid": 99, "ask": 100, "markPrice": 99.5}]}
    calls, clock = [], [0.0]

    def fetch(url: str):
        calls.append(url)
        if len(calls) > 1:
            raise TimeoutError("down")
        return tickers

    rates = ks.FundingRates(max_age=900.0, fetch=fetch, clock=lambda: clock[0])
    assert rates("BTC/USD") == pytest.approx(0.4025 / 85_000 * 24 * 100) and rates("QNT/USD") is None and len(calls) == 1  # 0.0114% a day; no rate published: unknown
    clock[0] = 1_000.0
    assert rates("BTC/USD") == pytest.approx(0.4025 / 85_000 * 24 * 100) and len(calls) == 2  # Kraken unreachable: the last known rate stands
    assert ks.record_tickers(root=tmp_path, now=NOW, fetch=lambda url: tickers) == 2
    stored = ks.load("funding", root=tmp_path)
    assert stored["symbol"].tolist() == ["PF_XBTUSD"] and stored["funding_pct_per_day"].iloc[0] == pytest.approx(0.011365, rel=1e-3)
    assert list(ks.load("tickers", root=tmp_path).columns) == list(ks.TICKER_COLUMNS)  # the spread file keeps its columns
