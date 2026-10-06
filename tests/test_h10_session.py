"""H10's trade construction (src/research/hypotheses/h10_session.py) on made-up 5-minute bars."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.research.hypotheses.h10_session import rule_returns, session_trades


def _bars(day: str, *, morning: float, afternoon: float) -> pd.DataFrame:
    """One New York day of 5-minute bars: flat at 100, `morning` log move over 09:30-11:30, flat to 11:35, then `afternoon` to 16:00."""
    local = pd.date_range(f"{day} 00:00", f"{day} 23:55", freq="5min", tz="America/New_York")
    minutes = local.hour * 60 + local.minute
    log_close = np.where(minutes + 5 <= 570, 0.0, np.where(minutes + 5 <= 690, morning * (minutes + 5 - 570) / 120.0,
                         np.where(minutes + 5 <= 695, morning, np.where(minutes + 5 <= 960, morning + afternoon * (minutes + 5 - 695) / 265.0, morning + afternoon))))
    close = 100.0 * np.exp(log_close)
    return pd.DataFrame({"open": np.concatenate([[100.0], close[:-1]]), "close": close}, index=local.tz_convert("UTC"))


def test_the_signal_and_the_hold_are_measured_in_new_york_time_across_daylight_saving() -> None:
    summer, winter = _bars("2024-07-10", morning=0.010, afternoon=0.004), _bars("2024-12-11", morning=-0.006, afternoon=0.003)  # the open is 13:30 UTC, then 14:30 UTC
    saturday = _bars("2024-07-13", morning=0.02, afternoon=0.02)
    trades = session_trades(pd.concat([summer, saturday, winter]))
    assert [str(day.date()) for day in trades.index] == ["2024-07-10", "2024-12-11"]  # weekdays only
    assert trades["signal"].tolist() == pytest.approx([0.010, -0.006]) and trades["hold"].tolist() == pytest.approx([0.004, 0.003])
    net = rule_returns(trades, cost_bps=11.0)
    assert net.tolist() == pytest.approx([0.004 - 0.0011, -0.003 - 0.0011])  # with the morning: right in July, wrong in December
    assert rule_returns(trades, direction=-1.0).tolist() == pytest.approx([-0.004, 0.003])  # the mirror rule
    hour = session_trades(pd.concat([summer, winter]), signal_end="10:30", exit="20:00")
    assert hour["signal"].tolist() == pytest.approx([0.005, -0.003])  # half of the morning's move by 10:30
    assert hour["hold"].iloc[0] == pytest.approx(0.010 * (120 - 65) / 120 + 0.004)  # entered at 10:35, so the rest of the morning's move is in the hold


def test_a_day_with_a_missing_price_is_left_out_and_a_zero_signal_costs_nothing() -> None:
    day = _bars("2024-07-10", morning=0.01, afternoon=0.004)
    assert session_trades(day[day.index.tz_convert("America/New_York").hour != 15]).empty  # no bar ends at 16:00
    flat = _bars("2024-07-11", morning=0.0, afternoon=0.004)
    assert rule_returns(session_trades(flat), cost_bps=11.0).tolist() == [0.0]
