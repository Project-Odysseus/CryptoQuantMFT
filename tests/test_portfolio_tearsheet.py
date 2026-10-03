"""The book tearsheet (src/portfolio/tearsheet.py): the page, its tables, and the inputs built from runtime snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.portfolio.tearsheet import monthly_returns, snapshot_inputs, summary_table, tearsheet_html

DAYS = pd.date_range("2024-01-01", periods=200, freq="D", tz="UTC")


def _series(seed: int, drift: float = 0.001) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(100.0 * np.exp(np.cumsum(rng.normal(drift, 0.02, len(DAYS)))), index=DAYS)


def test_the_summary_matches_hand_calculation() -> None:
    returns = pd.Series([0.10, -0.50, 0.20, 0.0], index=DAYS[:4])
    table = summary_table(returns, returns * 2.0, "BTC/USD")
    assert table.loc["total return", "book"] == pytest.approx(1.1 * 0.5 * 1.2 - 1.0)
    assert table.loc["max drawdown", "book"] == pytest.approx(0.5) and table.loc["worst day", "book"] == pytest.approx(0.5)
    assert table.loc["beta to the benchmark", "book"] == pytest.approx(0.5) and table.loc["days", "buy-and-hold BTC/USD"] == 4
    assert np.isnan(table.loc["beta to the benchmark", "buy-and-hold BTC/USD"])
    assert list(summary_table(returns, None, "x").columns) == ["book"]


def test_monthly_returns_compound_within_each_month() -> None:
    returns = pd.Series(0.01, index=pd.date_range("2024-01-30", periods=4, freq="D", tz="UTC"))
    table = monthly_returns(returns)
    assert table.loc[2024, 1] == pytest.approx(1.01**2 - 1) and table.loc[2024, 2] == pytest.approx(1.01**2 - 1)
    assert list(table.columns) == list(range(1, 13)) and np.isnan(table.loc[2024, 3])


def test_the_page_holds_every_section_and_needs_a_few_days_of_history() -> None:
    equity = _series(1)
    units = pd.DataFrame({"trend_a": equity.pct_change(), "trend_b": equity.pct_change() * 0.5 + 0.001, "idle": 0.0}).dropna()
    exposure = pd.DataFrame({"gross": 0.8, "net": 0.4, "beta_exposure": 0.5, "volatility": 0.3, "stressed_volatility": 0.35}, index=DAYS)
    page = tearsheet_html("unit <book>", equity, benchmark=_series(2), benchmark_name="BTC/USD", unit_returns=units, exposure=exposure, notes=["a note & more"])
    for section in ("Equity", "Drawdown", "Monthly returns", "Rolling risk", "Exposure", "Strategy correlations"):
        assert f"<h2>{section}</h2>" in page
    assert page.count("data:image/png;base64,") == 6 and "unit &lt;book&gt;" in page and "a note &amp; more" in page
    assert "The 2 strategies are worth 1.0 independent bets" in page  # the idle one never moved; the other two are one bet
    assert "buy-and-hold BTC/USD" in page and "max drawdown" in page

    bare = tearsheet_html("bare", equity)
    assert "<h2>Exposure</h2>" not in bare and "<h2>Strategy correlations</h2>" not in bare and bare.count("data:image/png;base64,") == 4
    with pytest.raises(ValueError, match="three days"):
        tearsheet_html("short", equity.iloc[:2])


def _snapshots(count: int = 12) -> list[dict]:
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    out = []
    for day in range(count):
        equity = 1000.0 + 10.0 * day
        out.append({"timestamp": (start + timedelta(days=day)).isoformat(), "equity": equity,
                    "instruments": {"kraken_futures:BTC/USD": {"price": 60_000.0 + 500.0 * day}, "kraken_futures:ETH/USD": {"price": 0.0}},
                    "sleeves": {"btc_ma": {"pnl": 8.0 * day}, "eth_ma": {"pnl": 2.0 * day}},
                    "exposure": {"gross": 0.5, "net": 0.5, "beta_exposure": 0.6, "volatility": None, "stressed_volatility": None} if day else None})
    return out


def test_runtime_snapshots_become_tearsheet_inputs() -> None:
    inputs = snapshot_inputs(list(reversed(_snapshots())))  # stored newest first
    assert inputs["benchmark_name"] == "kraken_futures:BTC/USD" and inputs["benchmark"].iloc[-1] == 65_500.0
    assert inputs["equity"].iloc[0] == 1000.0 and inputs["equity"].index.is_monotonic_increasing
    assert inputs["unit_returns"]["btc_ma"].iloc[0] == pytest.approx(8.0 / 1000.0)  # the day's P&L change over yesterday's equity
    assert inputs["exposure"]["gross"].iloc[-1] == 0.5 and np.isnan(inputs["exposure"]["gross"].iloc[0])  # a snapshot from before the summary existed
    page = tearsheet_html("running", **inputs)
    assert "<h2>Exposure</h2>" in page and "<h2>Strategy correlations</h2>" in page
    with pytest.raises(ValueError, match="no snapshots"):
        snapshot_inputs([])


def test_the_cli_writes_a_tearsheet_from_the_database(tmp_path, capsys) -> None:
    import main
    from config import settings
    from src.storage.trade_logger import TradeLogger

    out = tmp_path / "sheet.html"
    assert main.write_portfolio_tearsheet("config/portfolio.example.toml", str(out)) == 0
    assert "Not enough history" in capsys.readouterr().out and not out.exists()
    logger = TradeLogger(database_path=settings.database_path)
    for snap in _snapshots():
        logger.log_portfolio_snapshot(timestamp=datetime.fromisoformat(snap["timestamp"]), snapshot={"portfolio": "trend-core", "gross": 0.5, "net": 0.5, "drawdown": 0.0, **snap})
    assert main.write_portfolio_tearsheet("config/portfolio.example.toml", str(out)) == 0
    assert "<h1>trend-core: running book</h1>" in out.read_text() and f"Wrote {out}" in capsys.readouterr().out
