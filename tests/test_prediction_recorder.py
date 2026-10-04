"""The prediction-market recorder (src/data/prediction_recorder.py), with fake venues. No network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from src.data.prediction_recorder import PredictionRecorder
from src.research.prediction_markets import BinaryMarket

NOW = datetime(2026, 10, 4, 19, 0, tzinfo=timezone.utc)


def _market(venue: str, market_id: str, bid, ask, *, hours: float = 1.0, token: str = "") -> BinaryMarket:
    return BinaryMarket(venue=venue, market_id=market_id, question=f"BTC above 85,000? ({market_id})", underlying="BTC", kind="above", expiry=NOW + timedelta(hours=hours),
                        floor=85_000.0, yes_bid=bid, yes_ask=ask, volume=100.0, fee_rate=0.07, averaging_seconds=60.0, settles_on="an index", token_ids=(token, token + "-no") if token else ())


class FakeVenue:
    def __init__(self, markets, *, fail_books=()) -> None:
        self.markets, self.fail_books, self.lists, self.books = markets, set(fail_books), 0, []

    def price_markets(self, underlying, **_settings):
        self.lists += 1
        return list(self.markets)

    def order_book(self, key):
        self.books.append(key)
        if key in self.fail_books:
            raise TimeoutError("down")
        return pd.DataFrame({"side": ["bid"] * 7 + ["ask"] * 2, "price": [0.60, 0.59, 0.58, 0.57, 0.56, 0.55, 0.54, 0.62, 0.63], "size": [10.0] * 9})

    def settled(self, series, *, limit=200):
        return pd.DataFrame([{"ticker": f"{series}-A", "open_time": NOW, "close_time": NOW, "floor": 85_000.0, "settled_at": 85_100.0, "yes": True, "volume": 1.0}])


def _recorder(tmp_path, kalshi, polymarket, **settings) -> PredictionRecorder:
    return PredictionRecorder(root=tmp_path, coins=("BTC",), kalshi=kalshi, polymarket=polymarket, spot=lambda coin: 85_388.0, **settings)


def test_one_cycle_stores_quotes_depth_markets_and_results(tmp_path) -> None:
    kalshi = FakeVenue([_market("kalshi", "K-NEAR", 0.60, 0.62), _market("kalshi", "K-DEAD", 0.0, 0.01), _market("kalshi", "K-ONE-SIDED", None, 0.5),
                        _market("kalshi", "K-FAR", 0.40, 0.45, hours=100), _market("kalshi", "K-OVER", 0.5, 0.6, hours=-1)])
    polymarket = FakeVenue([_market("polymarket", "P-1", 0.30, 0.35, token="tok1"), _market("polymarket", "P-BROKEN", 0.50, 0.55, token="tok2")], fail_books={"tok2"})
    recorder = _recorder(tmp_path, kalshi, polymarket)
    report = recorder.cycle(NOW)
    assert report["errors"] == [] and report["results"] == 2  # one settled market per Kalshi series (above, up_down)

    quotes = pd.read_csv(tmp_path / "quotes" / "2026-10-04.csv")
    assert quotes["market_id"].tolist() == ["K-NEAR", "K-FAR", "P-1"]  # two-sided and between 1% and 99%; an expired market is left out
    assert quotes.loc[0, ["yes_bid", "yes_ask", "spot"]].tolist() == [0.60, 0.62, 85_388.0]
    assert quotes.loc[2, ["yes_bid", "yes_ask"]].tolist() == [0.60, 0.62]  # Polymarket's quote is the top of the book just read, not the listed one

    depth = pd.read_csv(tmp_path / "depth" / "2026-10-04.csv")
    assert set(depth["market_id"]) == {"K-NEAR", "P-1"}  # K-FAR settles beyond the horizon; the broken book is skipped, the rest kept
    near = depth[depth["market_id"] == "K-NEAR"]
    assert near[near["side"] == "bid"]["level"].tolist() == [1, 2, 3, 4, 5] and near[near["side"] == "ask"]["price"].tolist() == [0.62, 0.63]  # five levels a side at most
    assert kalshi.books == ["K-NEAR"] and polymarket.books == ["tok1", "tok2"]  # Polymarket's books are asked for by token

    markets = pd.read_csv(tmp_path / "markets.csv")
    assert markets["market_id"].tolist() == ["K-NEAR", "K-FAR", "P-1"] and markets.loc[2, "yes_token"] == "tok1" and markets.loc[0, "averaging_seconds"] == 60.0
    assert len(pd.read_csv(tmp_path / "results" / "kalshi.csv")) == 2


def test_later_cycles_append_and_repeat_nothing_that_is_already_stored(tmp_path) -> None:
    kalshi, polymarket = FakeVenue([_market("kalshi", "K-NEAR", 0.60, 0.62)]), FakeVenue([_market("polymarket", "P-1", 0.30, 0.35, token="tok1")])
    recorder = _recorder(tmp_path, kalshi, polymarket, list_every=1800.0)
    recorder.cycle(NOW)
    second = recorder.cycle(NOW + timedelta(minutes=10))
    assert second["results"] == 0  # results are checked hourly
    assert len(pd.read_csv(tmp_path / "quotes" / "2026-10-04.csv")) == 4 and len(pd.read_csv(tmp_path / "markets.csv")) == 2  # a market is described once
    assert kalshi.lists == 2 and polymarket.lists == 1  # Polymarket's large market list is refreshed every half hour, its books every cycle
    recorder.cycle(NOW + timedelta(minutes=70))
    assert polymarket.lists == 2 and len(pd.read_csv(tmp_path / "results" / "kalshi.csv")) == 2  # asked again after an hour: nothing new

    restarted = _recorder(tmp_path, kalshi, polymarket)  # a new process doesn't describe known markets or results again
    restarted.cycle(NOW + timedelta(minutes=80))
    assert len(pd.read_csv(tmp_path / "markets.csv")) == 2 and len(pd.read_csv(tmp_path / "results" / "kalshi.csv")) == 2


def test_one_venue_failing_leaves_the_others_rows(tmp_path) -> None:
    class Down(FakeVenue):
        def price_markets(self, underlying, **_settings):
            raise ConnectionError("kalshi is down")

        def settled(self, series, *, limit=200):
            raise ConnectionError("kalshi is down")

    report = _recorder(tmp_path, Down([]), FakeVenue([_market("polymarket", "P-1", 0.30, 0.35, token="tok1")])).cycle(NOW)
    assert len(report["errors"]) == 2 and report["errors"][0].startswith("kalshi BTC: ConnectionError") and report["quotes"] == 1
    assert pd.read_csv(tmp_path / "quotes" / "2026-10-04.csv")["venue"].tolist() == ["polymarket"]
