"""The shared H4/H5 panel (src/research/hypotheses/binary_panel.py): implied probabilities, the panel, and both hypotheses' statistics. No network."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.research import prediction_markets as pm
from src.research.hypotheses import binary_panel as bp

NOW = datetime(2026, 9, 30, 9, 30, tzinfo=timezone.utc)
CLOSE = datetime(2026, 9, 30, 21, 0, tzinfo=timezone.utc)
SPOT = 84_000.0


def _chain(now: datetime = NOW, *, skew: float = 0.0, spot: float = SPOT) -> pd.DataFrame:
    """Two expiries, strikes every 500, volatility `0.40 + skew x (K/S - 1)` (a negative skew: higher volatility below)."""
    rows = []
    for days, base in ((1, 0.40), (7, 0.50)):
        expiry = pd.Timestamp(now).normalize() + pd.Timedelta(days=days, hours=8)
        for strike in np.arange(80_000.0, 88_500.0, 500.0):
            vol = base + skew * (strike / spot - 1.0)
            for right in ("call", "put"):
                rows.append({"timestamp": pd.Timestamp(now), "expiry": expiry, "strike": strike, "right": right, "mark_iv": vol, "index_price": spot,
                             "mark": max(spot - strike, 0.0) + 300.0 if right == "call" else max(strike - spot, 0.0) + 300.0, "bid": 0.0, "ask": 0.0})
    frame = pd.DataFrame(rows)
    frame["bid"], frame["ask"] = frame["mark"] - 20.0, frame["mark"] + 20.0
    return frame


def test_the_daily_event_ticker() -> None:
    assert bp.daily_event_ticker(date(2026, 9, 30)) == "KXBTCD-26SEP3017" and bp.daily_event_ticker(date(2026, 10, 4)) == "KXBTCD-26OCT0417"


def test_the_implied_probability_matches_n_d2_on_a_flat_smile_and_moves_with_the_skew() -> None:
    flat_chain = _chain()
    smile, flat = bp.implied_prob_above(flat_chain, 84_500.0, CLOSE, now=NOW, spot=SPOT)
    assert smile == pytest.approx(flat, abs=2e-3) and flat == pytest.approx(pm.prob_above(SPOT, 84_500.0, 0.40, (CLOSE - NOW).total_seconds()))
    skewed, skew_flat = bp.implied_prob_above(_chain(skew=-2.0), 84_500.0, CLOSE, now=NOW, spot=SPOT)
    assert skewed > skew_flat + 0.01  # volatility falling in the strike makes calls above cheaper: more probability above than N(d2) says
    assert bp.implied_prob_above(flat_chain, 83_000.0, NOW, now=NOW, spot=SPOT) == (1.0, 1.0) and bp.implied_prob_above(flat_chain, 85_000.0, NOW, now=NOW, spot=SPOT) == (0.0, 0.0)
    low, _ = bp.implied_prob_above(flat_chain, 82_000.0, CLOSE, now=NOW, spot=SPOT)
    high, _ = bp.implied_prob_above(flat_chain, 86_000.0, CLOSE, now=NOW, spot=SPOT)
    assert 0.5 < low <= 1.0 and 0.0 <= high < 0.5 and low > smile > high


def test_the_put_spread_is_priced_per_unit_of_payout() -> None:
    cost = bp.put_spread_cost(_chain(), 83_750.0, CLOSE)
    # puts at 84,000 (mark 300) and 83,500 (mark 300): buy the upper at its ask, sell the lower at its bid, over 500
    assert cost["buy"] == pytest.approx((320.0 - 280.0) / 500.0) and cost["mark"] == pytest.approx(0.0) and cost["gap_hours"] == pytest.approx(11.0)
    assert bp.put_spread_cost(_chain(), 70_000.0, CLOSE) is None


class FakeKalshi:
    """One daily event with levels every 250 around the price; quotes are a fixed function of the level."""

    def __init__(self, settle: float | None = 84_300.0) -> None:
        self.settle, self.calls = settle, []

    def event_markets(self, event: str) -> list[dict]:
        self.calls.append(("event", event))
        if event != "KXBTCD-26SEP3017":
            return []
        return [{"ticker": f"{event}-T{strike - 0.01:.2f}", "event_ticker": event, "floor_strike": strike - 0.01, "open_time": "2026-09-29T20:00:00Z", "close_time": "2026-09-30T21:00:00Z",
                 "result": ("yes" if self.settle > strike - 0.01 else "no") if self.settle is not None else "", "expiration_value": str(self.settle) if self.settle else None}
                for strike in np.arange(80_000.0, 88_250.0, 250.0)]

    def quotes_at(self, tickers, when):
        self.calls.append(("quotes", len(tickers)))
        out = {}
        for ticker in tickers:
            strike = float(ticker.rsplit("-T", 1)[1])
            mid = float(np.clip(0.5 - (strike - SPOT) / 2_000.0, 0.02, 0.98))
            if strike < 86_000:  # the far levels have no two-sided quote
                out[ticker] = (round(mid - 0.01, 4), round(mid + 0.01, 4))
        return out


def test_the_panel_has_one_row_per_snapshot_and_quoted_level_in_range(tmp_path) -> None:
    client = FakeKalshi()
    chains = pd.concat([_chain(NOW), _chain(NOW + timedelta(hours=2)), _chain(CLOSE - timedelta(minutes=20))])  # the last is under an hour from settling
    panel = bp.build_panel(chains, client, cache_dir=tmp_path)
    assert panel["time"].nunique() == 2 and set(panel["event"]) == {"KXBTCD-26SEP3017"}
    assert panel["log_moneyness"].abs().max() <= 0.03 and panel["strike"].max() < 86_000  # +-3% of 84,000 is 81,518 to 86,558; the unquoted levels are left out
    row = panel[(panel["time"] == pd.Timestamp(NOW)) & (panel["strike"] == 83_999.99)].iloc[0]
    assert row["hours_left"] == pytest.approx(11.5) and row["mid"] == pytest.approx(0.5, abs=1e-3) and row["yes"] == True and row["settled_at"] == 84_300.0  # noqa: E712
    assert 0.45 < row["implied"] < 0.55 and row["put_spread_gap_hours"] == pytest.approx(11.0)
    assert (panel.loc[panel["strike"] > 84_300, "yes"] == False).all()  # noqa: E712
    assert (tmp_path / "KXBTCD-26SEP3017.json").exists()  # a settled event is cached
    events_before = sum(1 for kind, _ in client.calls if kind == "event")
    bp.build_panel(chains.iloc[: len(_chain())], client, cache_dir=tmp_path)
    assert sum(1 for kind, _ in client.calls if kind == "event") == events_before  # settled events come from the cache, the rest are fetched once per run

    open_panel = bp.build_panel(_chain(NOW), FakeKalshi(settle=None), cache_dir=tmp_path / "open")
    assert open_panel["yes"].isna().all() and not (tmp_path / "open" / "KXBTCD-26SEP3017.json").exists()  # unsettled: no outcome, not cached


def _panel(rng, events: int = 30, informative: float = 0.5) -> pd.DataFrame:
    """A synthetic panel where the truth lies `informative` of the way from Kalshi's mid to the options' probability."""
    rows = []
    for event in range(events):
        base = pd.Timestamp("2026-10-05", tz="UTC") + pd.Timedelta(days=event)
        final = rng.normal(0.0, 0.01)  # the day's return decides every level at once
        for strike_shift in np.linspace(-0.02, 0.02, 9):
            true_prob = float(np.clip(0.5 - strike_shift / 0.02 * 0.4, 0.03, 0.97))
            noise = rng.normal(0.0, 0.05)
            implied = float(np.clip(true_prob + (1 - informative) * noise, 0.01, 0.99))
            mid = float(np.clip(true_prob - informative * noise, 0.02, 0.98))
            rows.append({"time": base, "event": f"E{event}", "ticker": f"E{event}-{strike_shift:+.3f}", "strike": 84_000 * (1 + strike_shift), "index": 84_000.0, "hours_left": 10.0,
                         "log_moneyness": np.log(1 + strike_shift), "yes_bid": mid - 0.01, "yes_ask": mid + 0.01, "mid": mid, "implied": implied, "implied_flat": implied,
                         "put_spread_buy": 0.3, "put_spread_gap_hours": 11.0, "yes": bool(final > strike_shift), "settled_at": 84_000 * (1 + final)})
    return pd.DataFrame(rows)


def test_h5_recovers_how_much_of_the_gap_the_options_are_right_about() -> None:
    result = bp.h5_gap(_panel(np.random.default_rng(0), events=400, informative=0.5), runs=300)
    estimate, low, high = result["slope"]
    assert 0.2 < estimate < 0.8 and low > 0.0 and high < 1.1 and result["events"] == 400 and result["rows"] == 3600
    useless = bp.h5_gap(_panel(np.random.default_rng(1), events=400, informative=0.0), runs=300)["slope"]
    assert useless[0] < estimate and useless[1] < 0.15  # Kalshi already at the truth: the gap is the options' own noise
    assert list(result["by_moneyness"].columns) == ["rows", "kalshi_mid", "implied", "happened", "mean_gap", "mean_spread"] and result["by_moneyness"]["rows"].sum() == 3600
    assert np.isnan(bp.h5_gap(_panel(np.random.default_rng(2), events=2), runs=50)["slope"][1])  # two events: no interval


def test_the_taker_rule_buys_the_side_the_options_favour_only_beyond_the_margin() -> None:
    panel = pd.DataFrame([
        {"event": "A", "time": pd.Timestamp(NOW), "yes_bid": 0.49, "yes_ask": 0.51, "implied": 0.60, "yes": True, "log_moneyness": 0.0},   # buy Yes: 0.60 - 0.51 - fee > 0.02
        {"event": "A", "time": pd.Timestamp(NOW), "yes_bid": 0.49, "yes_ask": 0.51, "implied": 0.53, "yes": True, "log_moneyness": 0.0},   # edge 0.02 less the fee: no trade
        {"event": "B", "time": pd.Timestamp(NOW), "yes_bid": 0.69, "yes_ask": 0.71, "implied": 0.55, "yes": True, "log_moneyness": 0.0},   # buy No at 0.31
        {"event": "C", "time": pd.Timestamp(NOW), "yes_bid": 0.49, "yes_ask": 0.51, "implied": 0.70, "yes": None, "log_moneyness": 0.0},   # not settled: left out
    ])
    trades = bp.taker_trades(panel)
    assert trades["side"].tolist() == ["yes", "no"]
    assert trades["cost"].tolist() == pytest.approx([0.51 + 0.07 * 0.51 * 0.49, 0.31 + 0.07 * 0.31 * 0.69])
    assert trades["pnl"].tolist() == pytest.approx([1 - 0.51 - 0.07 * 0.51 * 0.49, 0 - 0.31 - 0.07 * 0.31 * 0.69])


def test_h4_prices_protection_just_below_the_price_and_its_return() -> None:
    panel = _panel(np.random.default_rng(3), events=200)
    result = bp.h4_protection(panel, runs=200)
    rows = panel[(panel["log_moneyness"] <= -0.005) & (panel["log_moneyness"] >= -0.02)]
    assert result["rows"] == len(rows) == result["settled_rows"] and result["events"] == 200
    no_ask = 1.0 - rows["yes_bid"]
    cost = no_ask + 0.07 * no_ask * (1 - no_ask)
    assert result["mean_cost"] == pytest.approx(cost.mean()) and result["breach_rate"] == pytest.approx(1.0 - rows["yes"].mean())
    assert result["pnl"][0] == pytest.approx(((1.0 - rows["yes"].astype(float)) - cost).mean())
    assert result["pnl"][0] < 0  # fairly priced before the spread and the fee: the buyer pays both
    assert result["richness"][0] == pytest.approx((cost - (1 - rows["implied"])).mean()) and result["mean_cover_bps"] > 0
    assert result["put_spread_rows"] == len(rows) and result["put_spread_buy"] == pytest.approx(0.3)
    assert bp.describe_interval(result["pnl"]).count("(") == 1 and bp.describe_interval((float("nan"),) * 3) == "n/a"
    assert len(bp.window(panel, "2026-10-10", "2026-10-12")) == 2 * 9


def test_the_confirmatory_window_runs_on_until_it_holds_enough_events() -> None:
    panel = _panel(np.random.default_rng(5), events=30)  # one event a day from 2026-10-05
    rows, end = bp.confirmatory_rows(panel, "2026-10-05", "2026-10-25", min_events=20)
    assert end == "2026-10-25" and rows["event"].nunique() == 20  # exactly enough by the planned end
    rows, end = bp.confirmatory_rows(panel, "2026-10-05", "2026-10-15", min_events=20)
    assert end == "2026-10-25" and rows["event"].nunique() == 20  # ten by the planned end: the end moves to the day the 20th is in
    rows, end = bp.confirmatory_rows(panel, "2026-10-05", "2026-10-15", min_events=40)
    assert end is None and rows["event"].nunique() == 30  # not ready: everything recorded so far, and no end
    unsettled = panel.assign(yes=None)
    assert bp.confirmatory_rows(unsettled, "2026-10-05", "2026-10-25", min_events=20)[1] is None  # quotes without outcomes don't count
    assert bp.confirmatory_rows(panel.iloc[:0], "2026-10-05", "2026-10-25")[1] is None

