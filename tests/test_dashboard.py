"""The local dashboard (src/dashboard): what it reads from the database and state folders, and its web endpoints."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.dashboard.data import DashboardData, clean, unit_of
from src.dashboard.server import STATIC, build_app
from src.storage.trade_logger import TradeLogger

START = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
BTC = "kraken_futures:BTC/USD"


def _snapshot(name: str, equity: float, btc_price: float, *, mode: str = "paper") -> dict:
    return {"portfolio": name, "mode": mode, "equity": equity, "initial_equity": 10_000.0, "gross": 0.5, "net": 0.4, "drawdown": max(0.0, 1 - equity / 10_000.0),
            "instruments": {BTC: {"units": 0.05, "price": btc_price, "weight": 0.42, "target": 0.42}, "kraken_futures:ADA/USD": {"units": 0.0, "price": 0.24, "weight": 0.0, "target": None}},
            "sleeves": {"btc_ma_1d": {"pnl": 12.0, "strategy": "moving_average_crossover"}, "taker__ada": {"pnl": 1.0}, "taker__dot": {"pnl": -3.0}},
            "exposure": {"benchmark": BTC, "beta_exposure": 0.45, "volatility": 0.2, "stressed_volatility": float("nan")}, "strategy_correlation": {"effective_bets": 2.0}}


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "books.db"
    logger = TradeLogger(database_path=path)
    for hour, (equity, btc) in enumerate([(10_000.0, 80_000.0), (10_050.0, 80_400.0), (10_020.0, 81_000.0)]):
        logger.log_portfolio_snapshot(timestamp=START + timedelta(hours=hour), snapshot=_snapshot("multi", equity, btc))
    logger.log_portfolio_snapshot(timestamp=START, snapshot=_snapshot("multi-live", 19.1, 80_000.0, mode="live"))
    fill = {"instrument": BTC, "side": "buy", "units": 0.05, "price": 80_010.0, "fee": 2.0, "reason": "open", "sleeves": {"btc_ma_1d": 0.03, "taker__ada": 0.02}}
    logger.log_event(timestamp=START, level="INFO", event_type="portfolio_fill", message="buy 0.05 BTC (old row, no book named)", source="portfolio", metadata=fill)
    logger.log_event(timestamp=START + timedelta(hours=1), level="INFO", event_type="portfolio_fill", message="buy 0.05 BTC", source="portfolio", metadata={**fill, "portfolio": "multi"})
    logger.log_event(timestamp=START + timedelta(hours=1), level="INFO", event_type="portfolio_fill", message="another book's", source="portfolio", metadata={**fill, "portfolio": "other"})
    logger.log_event(timestamp=START + timedelta(hours=2), level="WARNING", event_type="portfolio_alert", message="beta is close to its cap", source="portfolio", metadata={"portfolio": "multi"})
    logger.log_event(timestamp=START + timedelta(hours=2), level="WARNING", event_type="portfolio_alert", message="the other book's alert", source="portfolio", metadata={"portfolio": "other"})
    logger.log_event(timestamp=START + timedelta(hours=3), level="INFO", event_type="portfolio_alert_cleared", message="feed is back", source="portfolio", metadata={})
    return path


def test_books_history_fills_and_alerts_come_from_the_database(tmp_path: Path) -> None:
    data = DashboardData(_database(tmp_path), tmp_path)
    books = {book["name"]: book for book in data.books(now=START + timedelta(hours=3))}
    assert set(books) == {"multi", "multi-live"} and books["multi-live"]["mode"] == "live"
    multi = books["multi"]
    assert multi["snapshots"] == 3 and multi["equity"] == 10_020.0 and multi["change"] == pytest.approx(0.002) and multi["positions"] == 1
    assert multi["reporting"] and multi["age_seconds"] == 3600.0 and not data.books(now=START + timedelta(hours=9))[0]["reporting"]  # silent for hours: stopped

    latest = data.book("multi")
    assert latest["timestamp"].startswith("2026-10-03T14:00") and latest["exposure"]["stressed_volatility"] is None  # NaN becomes null for the browser
    assert data.book("nope") is None
    json.dumps(latest, allow_nan=False)

    history = data.history("multi")
    assert history["equity"] == [10_000.0, 10_050.0, 10_020.0] and history["benchmark"] == [80_000.0, 80_400.0, 81_000.0] and history["benchmark"] and history["name"] == "multi"
    assert history["strategies"] == {"btc_ma_1d": [12.0] * 3, "taker": [-2.0] * 3}  # a basket's coins are one strategy
    assert history["weights"] == {BTC: [0.42] * 3} and history["beta"] == [0.45] * 3 and len(history["time"]) == 3
    thinned = data.history("multi", max_points=2)
    assert thinned["equity"][0] == 10_000.0 and thinned["equity"][-1] == 10_020.0  # the latest point is always kept
    assert data.history("nope")["time"] == []

    fills = data.fills("multi")
    assert [fill["message"] for fill in fills] == ["buy 0.05 BTC", "buy 0.05 BTC (old row, no book named)"]  # newest first; the unnamed one matched by its snapshot's time
    assert fills[0]["strategies"] == ["btc_ma_1d", "taker"] and fills[0]["price"] == 80_010.0 and data.fills("multi-live")[0]["message"].endswith("(old row, no book named)")
    alerts = data.alerts("multi")
    assert [alert["message"] for alert in alerts] == ["feed is back", "beta is close to its cap"] and alerts[0]["kind"] == "cleared" and len(data.alerts()) == 3
    assert unit_of("taker__ada") == "taker" and unit_of("btc_ma_1d") == "btc_ma_1d" and clean({"a": [float("inf"), 1.0]}) == {"a": [None, 1.0]}


def test_a_missing_database_and_stale_jobs_are_reported_not_raised(tmp_path: Path) -> None:
    data = DashboardData(tmp_path / "none.db", tmp_path)
    assert data.books() == [] and data.fills("x") == [] and data.alerts() == [] and data.history("x")["time"] == []
    state = tmp_path / "data/portfolio/multi-paper/engine.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}")
    (tmp_path / "data/portfolio/btc-drill").mkdir()
    (tmp_path / "data/portfolio/btc-drill/engine.json").write_text("{}")
    spreads = tmp_path / "data/market_data/kraken_spreads/tickers/2026-10-03.csv"
    spreads.parent.mkdir(parents=True)
    spreads.write_text("time,symbol\n")
    now = state.stat().st_mtime
    os.utime(spreads, (now - 7200, now - 7200))
    jobs = {job["job"]: job for job in data.health(now=now + 60)}
    assert jobs["book: multi-paper"]["ok"] and "book: btc-drill" not in jobs  # a drill's folder is not a running book
    assert not jobs["Kraken spreads"]["ok"] and jobs["Kraken spreads"]["seconds_since"] == pytest.approx(7260, abs=1)  # two hours silent: stale
    assert not jobs["option chains"]["ok"] and jobs["option chains"]["seconds_since"] is None  # never ran
    assert not data.health(now=now + 3600)[0]["ok"]  # a book whose checkpoint is an hour old has stopped
    research = data.research(now=datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert research == {"snapshots_cached": 0, "events_cached": 0, "confirmatory_run_on": "2026-11-17", "days_until": 16, "done": False}


@pytest.mark.asyncio
async def test_the_server_only_reads(tmp_path: Path) -> None:
    app = build_app(DashboardData(_database(tmp_path), tmp_path))
    assert {route.method for route in app.router.routes()} <= {"GET", "HEAD"}  # nothing can be changed through the dashboard
    async with TestClient(TestServer(app)) as client:
        books = await (await client.get("/api/books")).json()
        assert [book["name"] for book in books] == ["multi", "multi-live"]
        assert (await (await client.get("/api/book/multi")).json())["equity"] == 10_020.0
        assert (await client.get("/api/book/nope")).status == 404
        assert len((await (await client.get("/api/book/multi/history")).json())["time"]) == 3
        assert len(await (await client.get("/api/book/multi/fills")).json()) == 2
        assert len(await (await client.get("/api/alerts", params={"book": "multi"})).json()) == 2
        system = await (await client.get("/api/system")).json()
        assert set(system) == {"health", "research", "costs"} and system["costs"] == []
        page = await client.get("/")
        assert page.status == 200 and "CryptoQuant books" in await page.text()
        assert (await client.post("/api/books")).status == 405
        for asset in ("app.js", "vendor/react.production.min.js", "vendor/react-dom.production.min.js", "vendor/htm.min.js"):
            assert (STATIC / asset).exists() and (await client.get(f"/static/{asset}")).status == 200  # no file comes from the internet


def test_the_live_value_adds_each_positions_move_since_the_bar_close(tmp_path: Path) -> None:
    calls = []

    def fetch(url: str):
        calls.append(url)
        return {"tickers": [{"symbol": "PF_XBTUSD", "markPrice": 82_000.0}, {"symbol": "PF_ADAUSD", "markPrice": 0.30}]}

    data = DashboardData(_database(tmp_path), tmp_path, fetch=fetch)
    live = data.live("multi")  # stored: 0.05 BTC valued at 81,000, equity 10,020; ADA not held
    assert live["bar_close_equity"] == 10_020.0 and live["since_bar_close"] == pytest.approx(0.05 * 1_000.0) and live["equity"] == pytest.approx(10_070.0)
    assert live["change"] == pytest.approx(0.007) and live["instruments"][BTC]["move"] == pytest.approx(82_000 / 81_000 - 1) and live["instruments"]["kraken_futures:ADA/USD"]["pnl"] == 0.0
    assert data.live("multi")["equity"] == pytest.approx(10_070.0) and len(calls) == 1  # one request a minute serves every book
    assert data.live("nope") is None

    def down(url: str):
        raise TimeoutError("no network")

    assert DashboardData(_database(tmp_path), tmp_path, fetch=down).live("multi") is None  # the page then shows the bar-close value only
