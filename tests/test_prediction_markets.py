"""Prediction-market research tools (src/research/prediction_markets.py): parsing, fair value, fees, hedges, calibration. No network."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.research import prediction_markets as pm

NOW = datetime(2026, 10, 3, 21, 0, tzinfo=timezone.utc)
POLY_UPDOWN = {"question": "Bitcoin Up or Down - October 3, 6:15PM-6:30PM ET", "slug": "btc-updown-15m-1791065700", "outcomes": '["Up", "Down"]',
               "clobTokenIds": '["111", "222"]', "endDate": "2026-10-03T22:30:00Z", "eventStartTime": "2026-10-03T22:15:00Z", "bestBid": 0.49, "bestAsk": 0.5,
               "volume": "8718.3", "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-60s-streams", "feesEnabled": True,
               "feeSchedule": {"exponent": 1, "rate": 0.07, "takerOnly": True}, "active": True, "closed": False}
POLY_ABOVE = {"question": "Will the price of Bitcoin be above $84,000 on October 4?", "slug": "bitcoin-above-84k-on-october-4", "outcomes": '["Yes", "No"]',
              "clobTokenIds": '["333", "444"]', "endDate": "2026-10-04T16:00:00Z", "bestBid": 0.61, "bestAsk": 0.63, "volume": 5000,
              "resolutionSource": "https://www.binance.com/en/trade/BTC_USDT", "feesEnabled": False, "active": True, "closed": False}
KALSHI_ABOVE = {"ticker": "KXBTCD-26OCT0318-T84599.99", "title": "Bitcoin price on Oct 3, 2026?", "yes_sub_title": "$84,600 or above", "strike_type": "greater",
                "floor_strike": 84599.99, "close_time": "2026-10-03T22:00:00Z", "yes_bid_dollars": "0.8800", "yes_ask_dollars": "0.8900", "volume_fp": "153454.17",
                "rules_primary": "If the simple average of the sixty seconds of CF Benchmarks' BRTI before 6 PM EDT is above 84599.99 ..."}
KALSHI_RANGE = {**KALSHI_ABOVE, "ticker": "KXBTC-26OCT0318-B84650", "strike_type": "between", "floor_strike": 84600, "cap_strike": 84699.99, "yes_bid_dollars": "0.3000", "yes_ask_dollars": "0.3400"}
KALSHI_15M = {**KALSHI_ABOVE, "ticker": "KXBTC15M-26OCT031730-30", "strike_type": "greater_or_equal", "floor_strike": 84670.39, "open_time": "2026-10-03T21:15:00Z",
              "close_time": "2026-10-03T21:30:00Z", "yes_bid_dollars": "0.7400", "yes_ask_dollars": "0.7500"}


def test_both_venues_markets_come_out_in_one_shape() -> None:
    up = pm.parse_polymarket(POLY_UPDOWN, "BTC")
    assert (up.venue, up.kind, up.floor, up.fee_rate, up.averaging_seconds, up.token_ids) == ("polymarket", "up_down", None, 0.07, 60.0, ("111", "222"))
    assert up.start == datetime(2026, 10, 3, 22, 15, tzinfo=timezone.utc) and up.mid == pytest.approx(0.495) and up.no_ask == pytest.approx(0.51)
    assert up.with_reference(84_700.0).floor == 84_700.0
    above = pm.parse_polymarket(POLY_ABOVE, "BTC")
    assert (above.kind, above.floor, above.fee_rate, above.averaging_seconds) == ("above", 84_000.0, 0.0, 0.0)
    assert pm.parse_polymarket({**POLY_ABOVE, "question": "Will Bitcoin hit $100k in 2026?"}, "BTC") is None  # a one-touch: not priced here
    assert pm.parse_polymarket({**POLY_ABOVE, "outcomes": "not json"}, "BTC") is None

    k_above, k_range, k_15 = (pm.parse_kalshi(raw, "BTC") for raw in (KALSHI_ABOVE, KALSHI_RANGE, KALSHI_15M))
    assert (k_above.kind, k_above.floor, k_above.yes_bid, k_above.yes_ask, k_above.fee_rate) == ("above", 84599.99, 0.88, 0.89, 0.07)
    assert (k_range.kind, k_range.floor, k_range.cap) == ("between", 84600.0, 84699.99)
    assert (k_15.kind, k_15.floor, k_15.start) == ("up_down", 84670.39, datetime(2026, 10, 3, 21, 15, tzinfo=timezone.utc))
    assert pm.parse_kalshi({**KALSHI_ABOVE, "strike_type": "structured"}, "BTC") is None
    assert pm.parse_kalshi({**KALSHI_ABOVE, "yes_bid_dollars": "0.0000"}, "BTC").yes_bid is None  # no bid resting


def test_the_clients_read_public_endpoints_and_follow_kalshis_cursor() -> None:
    calls: list[str] = []

    def fetch(url: str):
        calls.append(url)
        if "public-search" in url:
            return {"events": [{"markets": [POLY_UPDOWN if "up" in url else POLY_ABOVE, {**POLY_ABOVE, "slug": "closed-one", "closed": True}]}]}
        if "/book?" in url:
            return {"bids": [{"price": "0.47", "size": "85"}, {"price": "0.49", "size": "976"}], "asks": [{"price": "0.52", "size": "60"}, {"price": "0.50", "size": "26"}]}
        if "prices-history" in url:
            return {"history": [{"t": 1791058693, "p": 0.495}, {"t": 1791058757, "p": 0.51}]}
        if "/orderbook" in url:
            return {"orderbook_fp": {"yes_dollars": [["0.7300", "10"], ["0.7400", "5"]], "no_dollars": [["0.2400", "7"], ["0.2500", "3"]]}}
        if "candlesticks" in url:
            return {"candlesticks": [{"end_period_ts": 1791061680, "yes_bid": {"close_dollars": "0.0290"}, "yes_ask": {"close_dollars": "0.0300"}, "price": {"close_dollars": "0.0290"}, "volume_fp": "12"}]}
        if "status=settled" in url:
            return {"markets": [{"ticker": "KXBTC15M-A", "open_time": "2026-10-03T21:00:00Z", "close_time": "2026-10-03T21:15:00Z", "floor_strike": 84739.29,
                                 "expiration_value": "84670.39", "result": "no", "volume_fp": "100"}]}
        if "cursor=next" in url:
            return {"markets": [KALSHI_15M], "cursor": ""}
        return {"markets": [KALSHI_ABOVE], "cursor": "next"}

    poly = pm.PolymarketClient(fetch)
    markets = poly.price_markets("BTC")
    assert [market.market_id for market in markets] == ["btc-updown-15m-1791065700", "bitcoin-above-84k-on-october-4"]  # the closed one is dropped, soonest first
    book = poly.order_book("111")
    assert book.iloc[0].to_dict() == {"side": "bid", "price": 0.49, "size": 976.0} and book[book["side"] == "ask"].iloc[0]["price"] == 0.50
    assert poly.price_history("111").tolist() == [0.495, 0.51]

    kalshi = pm.KalshiClient(fetch)
    assert [raw["ticker"] for raw in kalshi.raw_markets("KXBTCD")] == [KALSHI_ABOVE["ticker"], KALSHI_15M["ticker"]] and any("cursor=next" in url for url in calls)
    k_book = kalshi.order_book("X")
    assert k_book[k_book["side"] == "bid"].iloc[0]["price"] == 0.74 and k_book[k_book["side"] == "ask"].iloc[0]["price"] == pytest.approx(0.75)  # a No bid at 0.25 is a Yes offer at 0.75
    settled = kalshi.settled("KXBTC15M")
    assert settled.iloc[0]["yes"] == False and settled.iloc[0]["settled_at"] == 84670.39  # noqa: E712
    candles = kalshi.candles("KXBTC15M", "KXBTC15M-A", NOW, NOW + timedelta(minutes=15))
    assert candles.iloc[0]["yes_ask"] == 0.03 and candles.index[0] == pd.Timestamp(1791061680, unit="s", tz="UTC")
    closes = pm.binance_closes(fetch=lambda url: [[1791062340000, "1", "2", "0.5", "84718.0", "9", 1791062399999]])
    assert closes.iloc[0] == 84718.0 and closes.index[0] == pd.Timestamp(1791062400000, unit="ms", tz="UTC")  # stamped at the candle's close


def test_fair_value_matches_hand_calculation_and_behaves_at_the_edges() -> None:
    sigma, seconds = 0.5, 3600.0
    variance = sigma**2 * seconds / pm.SECONDS_PER_YEAR
    d2 = (math.log(100.0 / 101.0) - variance / 2) / math.sqrt(variance)
    assert pm.prob_above(100.0, 101.0, sigma, seconds) == pytest.approx(0.5 * (1 + math.erf(d2 / math.sqrt(2))))
    assert pm.prob_above(100.0, 100.0, sigma, seconds) == pytest.approx(0.5, abs=0.002)  # at the level: a coin flip, a hair under for the drift term
    assert pm.prob_above(100.0, 90.0, sigma, 0.0) == 1.0 and pm.prob_above(100.0, 110.0, sigma, 0.0) == 0.0 and pm.prob_above(100.0, 100.0, sigma, 0.0) == 1.0
    assert pm.prob_above(100.0, 101.0, sigma, 60.0) < pm.prob_above(100.0, 101.0, sigma, 3600.0) < 0.5  # more time, more chance of getting there
    assert pm.prob_between(100.0, 99.0, 101.0, sigma, seconds) == pytest.approx(pm.prob_above(100.0, 99.0, sigma, seconds) - pm.prob_above(100.0, 101.0, sigma, seconds))
    assert pm.prob_between(100.0, 0.01, 1e9, sigma, seconds) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="positive"):
        pm.prob_above(0.0, 100.0, sigma, seconds)


def test_an_averaged_settlement_shortens_the_effective_horizon() -> None:
    assert pm.effective_seconds(900.0, 60.0) == pytest.approx(900.0 - 60.0 + 20.0)
    assert pm.effective_seconds(30.0, 60.0) == pytest.approx(10.0) and pm.effective_seconds(900.0, 0.0) == 900.0 and pm.effective_seconds(-5.0, 60.0) == 0.0
    market = pm.parse_kalshi(KALSHI_15M, "BTC")
    now = market.expiry - timedelta(minutes=10)
    averaged = pm.fair_value(market, 84_600.0, 0.5, now)
    assert averaged < pm.prob_above(84_600.0, market.floor, 0.5, 600.0) < 0.5  # below the level: less variance, less chance of crossing
    assert pm.fair_value(pm.parse_polymarket(POLY_UPDOWN, "BTC"), 84_600.0, 0.5, now) is None  # no reference price yet
    ranged = pm.parse_kalshi(KALSHI_RANGE, "BTC")
    assert 0.0 < pm.fair_value(ranged, 84_650.0, 0.5, ranged.expiry - timedelta(minutes=30)) < 0.2


def test_delta_is_the_slope_of_the_probability_and_the_hedge_is_the_opposite_position() -> None:
    spot, strike, sigma, seconds = 84_700.0, 84_600.0, 0.5, 1800.0
    bump = 1.0
    slope = (pm.prob_above(spot + bump, strike, sigma, seconds) - pm.prob_above(spot - bump, strike, sigma, seconds)) / (2 * bump)
    assert pm.binary_delta(spot, strike, sigma, seconds) == pytest.approx(slope, rel=1e-4)
    assert pm.binary_delta(spot, strike, sigma, 60.0) > pm.binary_delta(spot, strike, sigma, seconds)  # sharper as expiry nears
    market = pm.parse_kalshi({**KALSHI_ABOVE, "floor_strike": strike}, "BTC")
    now = market.expiry - timedelta(seconds=seconds)
    units = pm.hedge_units(market, spot, sigma, now, contracts=1000.0)
    assert units < 0 and units == pytest.approx(-1000.0 * pm.binary_delta(spot, strike, sigma, pm.effective_seconds(seconds, 60.0)))
    hedged = 1000.0 * (pm.fair_value(market, spot + 20.0, sigma, now) - pm.fair_value(market, spot, sigma, now)) + units * 20.0
    assert abs(hedged) < 0.05 * 1000.0 * (pm.fair_value(market, spot + 20.0, sigma, now) - pm.fair_value(market, spot, sigma, now))  # a small move is neutralised


def test_fees_and_edges() -> None:
    assert pm.taker_fee(0.5, 0.07) == pytest.approx(0.0175) and pm.taker_fee(0.99, 0.07) == pytest.approx(0.000693) and pm.taker_fee(0.5, 0.0) == 0.0
    market = pm.parse_kalshi(KALSHI_15M, "BTC")  # yes 0.74 / 0.75
    inside = pm.edges(market, 0.745)
    assert inside["buy_yes"] < 0 and inside["buy_no"] < 0  # fair value inside the spread: both sides lose the spread and the fee
    rich = pm.edges(market, 0.80)
    assert rich["buy_yes"] == pytest.approx(0.80 - 0.75 - 0.07 * 0.75 * 0.25) and rich["buy_no"] < 0
    cheap = pm.edges(market, 0.65)
    assert cheap["buy_no"] == pytest.approx(0.35 - 0.26 - 0.07 * 0.26 * 0.74) and cheap["buy_yes"] < 0
    table = pm.quote_table([market, pm.parse_polymarket(POLY_UPDOWN, "BTC")], 84_700.0, 0.5, market.expiry - timedelta(minutes=5))
    assert list(table["venue"]) == ["kalshi", "polymarket"] and table.loc[0, "fair"] > 0.5 and pd.isna(table.loc[1, "fair"]) and table.loc[0, "hedge_coins_per_100"] < 0


def test_the_same_question_on_two_venues_is_matched_and_priced_as_a_pair() -> None:
    kalshi = pm.parse_kalshi({**KALSHI_ABOVE, "floor_strike": 84_000.0, "close_time": "2026-10-04T16:00:00Z", "yes_bid_dollars": "0.5500", "yes_ask_dollars": "0.5600"}, "BTC")
    poly = pm.parse_polymarket(POLY_ABOVE, "BTC")  # yes 0.61 / 0.63, same level and time
    other = pm.parse_kalshi(KALSHI_ABOVE, "BTC")  # another level and day
    assert pm.match_markets([kalshi, other], [poly]) == [(kalshi, poly)]
    # Yes on Kalshi at 0.56 plus No on Polymarket at 1 - 0.61 = 0.39 costs 0.95, less Kalshi's fee
    assert pm.locked_edge(kalshi, poly) == pytest.approx(1 - 0.56 - 0.39 - 0.07 * 0.56 * 0.44)
    assert pm.locked_edge(kalshi, kalshi) < 0  # one venue against itself only pays the spread and the fees


def _chain() -> pd.DataFrame:
    now = pd.Timestamp("2026-10-03T12:00:00Z")
    rows = []
    for days, vol in ((1, 0.40), (7, 0.50)):
        for strike, mark, bid, ask in ((84_000.0, 900.0, 880.0, 920.0), (85_000.0, 450.0, 430.0, 470.0)):
            rows.append({"timestamp": now, "expiry": now + pd.Timedelta(days=days), "strike": strike, "right": "call", "mark": mark, "bid": bid, "ask": ask, "mark_iv": vol})
    return pd.DataFrame(rows)


def test_a_deribit_chain_gives_a_volatility_and_a_call_spread_price_for_the_same_bet() -> None:
    chain = _chain()
    now = chain["timestamp"].iloc[0]
    assert pm.chain_implied_vol(chain, 84_500.0, now + pd.Timedelta(hours=6)) == pytest.approx(0.40)  # before the first expiry: its volatility
    between = pm.chain_implied_vol(chain, 84_500.0, now + pd.Timedelta(days=4))
    assert between == pytest.approx(math.sqrt((0.16 * 1 + (0.25 * 7 - 0.16 * 1) * 3 / 6) / 4))  # linear in total variance
    bounds = pm.call_spread_bounds(chain, 84_500.0, now + pd.Timedelta(hours=20))
    assert bounds["lower_strike"] == 84_000.0 and bounds["upper_strike"] == 85_000.0 and bounds["expiry"] == now + pd.Timedelta(days=1)
    assert bounds["gap_hours"] == pytest.approx(4.0) and bounds["mark"] == pytest.approx(0.45) and bounds["buy"] == pytest.approx(0.49) and bounds["sell"] == pytest.approx(0.41)
    assert pm.call_spread_bounds(chain, 99_000.0, now + pd.Timedelta(days=1)) is None  # no strike above it
    with pytest.raises(ValueError, match="no usable"):
        pm.chain_implied_vol(chain.assign(mark_iv=0.0), 84_500.0, now + pd.Timedelta(days=1))


def test_calibration_shows_a_good_model_and_exposes_an_overconfident_one() -> None:
    rng = np.random.default_rng(0)
    truth = rng.uniform(0.05, 0.95, 20_000)
    happened = rng.uniform(size=truth.size) < truth
    good = pm.calibration_table(truth, happened)
    assert (good["forecast"] - good["happened"]).abs().max() < 0.03 and good.attrs["brier"] < 0.25 and good["forecasts"].sum() == 20_000
    overconfident = pm.calibration_table(np.clip(0.5 + (truth - 0.5) * 2.5, 0.0, 1.0), happened)
    assert overconfident.attrs["brier"] > good.attrs["brier"] and overconfident.attrs["log_loss"] > good.attrs["log_loss"]
    assert overconfident.loc["90%-100%", "happened"] < overconfident.loc["90%-100%", "forecast"] - 0.1
    assert pm.calibration_table([np.nan], [True]).empty


def test_the_updown_backtest_is_calibrated_on_a_random_walk_and_uses_only_the_past() -> None:
    rng = np.random.default_rng(4)
    index = pd.date_range("2025-01-01", periods=30_000, freq="5min", tz="UTC")
    closes = pd.Series(100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.001, len(index)))), index=index)
    result = pm.backtest_updown_model(closes, window_bars=3, halflife_bars=100.0)
    assert set(result["elapsed"]) == {1, 2} and len(result) > 15_000
    table = pm.calibration_table(result["prob"], result["up"])
    assert (table.loc[table["forecasts"] > 300, "forecast"] - table.loc[table["forecasts"] > 300, "happened"]).abs().max() < 0.05
    assert table.attrs["brier"] < 0.21  # knowing the move so far beats a coin flip (0.25)
    too_calm = pm.calibration_table(pm.backtest_updown_model(closes, window_bars=3, halflife_bars=100.0, vol_scale=0.3)["prob"], result["up"])
    assert too_calm.attrs["log_loss"] > table.attrs["log_loss"]  # an underestimated volatility is overconfident
    changed = closes.copy()
    changed.iloc[20_000:] *= 1.5
    again = pm.backtest_updown_model(changed, window_bars=3, halflife_bars=100.0)
    early = result["time"] < index[19_990]
    pd.testing.assert_series_equal(result.loc[early, "prob"], again.loc[again["time"] < index[19_990], "prob"])


def test_the_model_is_lined_up_against_kalshis_own_quotes() -> None:
    opened = pd.Timestamp("2026-10-03T21:00:00Z")
    index = pd.date_range(opened - pd.Timedelta(minutes=120), periods=140, freq="1min", tz="UTC")
    rng = np.random.default_rng(1)
    closes = pd.Series(84_000.0 * np.exp(np.cumsum(rng.normal(0.0, 0.0004, len(index)))), index=index)

    def fetch(url: str):
        if "status=settled" in url:
            return {"markets": [{"ticker": "KXBTC15M-A", "open_time": "2026-10-03T21:00:00Z", "close_time": "2026-10-03T21:15:00Z", "floor_strike": 84_739.29,
                                 "expiration_value": "84800.00", "result": "yes", "volume_fp": "100"}]}
        return {"candlesticks": [{"end_period_ts": int((opened + pd.Timedelta(minutes=minute)).timestamp()), "yes_bid": {"close_dollars": "0.5500"}, "yes_ask": {"close_dollars": "0.5700"},
                                  "price": {"close_dollars": "0.5600"}, "volume_fp": "5"} for minute in range(1, 16)]}

    table = pm.kalshi_model_vs_market(pm.KalshiClient(fetch), closes)
    assert len(table) == 14 and table["minutes_left"].tolist() == list(range(14, 0, -1))  # the closing minute itself is left out
    assert table["market_mid"].iloc[0] == pytest.approx(0.56) and table["spread"].iloc[0] == pytest.approx(0.02) and table["yes"].all()
    moved = closes[opened + pd.Timedelta(minutes=5)] / closes[opened]
    assert (table["model"].iloc[4] > 0.5) == (moved > 1.0)  # the model follows the exchange's move since the open, not its level
    assert json.dumps(table["model"].tolist())  # plain floats
