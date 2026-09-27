"""The frozen final holdout, the trial ledger and run manifests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.research import governance
from src.storage.bar_aggregator import OHLCVBar


def _bars(start: datetime, days: int) -> list[OHLCVBar]:
    return [OHLCVBar(exchange="mock", symbol="BTC/USD", interval_seconds=86_400, timestamp=start + timedelta(days=day), open=1.0, high=1.0, low=1.0,
                     close=1.0 + day, volume=1.0) for day in range(days)]


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CQM_FINAL_HOLDOUT", raising=False)


def test_long_series_are_frozen_from_the_fixed_date(frozen: None) -> None:
    bars = governance.trim_bars(_bars(datetime(2024, 6, 1, tzinfo=timezone.utc), 800))
    assert bars[-1].timestamp < governance.FINAL_HOLDOUT_START <= bars[-1].timestamp + timedelta(days=1)


def test_short_recent_series_keep_their_own_last_quarter_frozen(frozen: None) -> None:
    bars = _bars(datetime(2026, 6, 1, tzinfo=timezone.utc), 100)
    kept = governance.trim_bars(bars)
    assert len(kept) == 75


def test_unlocking_is_explicit_and_every_look_is_logged(frozen: None) -> None:
    bars = _bars(datetime(2024, 6, 1, tzinfo=timezone.utc), 800)
    with governance.final_holdout("final test of the BTC trend book"):
        assert len(governance.trim_bars(bars, label="BTC 1d")) == 800
        governance.trim_bars(bars, label="BTC 1d")  # the same look again isn't logged twice
    assert len(governance.trim_bars(bars)) < 800  # frozen again afterwards
    looks = [entry for entry in governance.ledger_entries() if entry["type"] == "final_holdout_look"]
    assert [(look["reason"], look["data"]) for look in looks] == [("final test of the BTC trend book", "BTC 1d")]
    with pytest.raises(ValueError):
        with governance.final_holdout(" "):
            pass


def test_frames_are_trimmed_by_their_time_column(frozen: None) -> None:
    frame = pd.DataFrame({"date": pd.date_range("2023-01-01", "2026-06-30", freq="D", tz="UTC"), "close": 1.0})
    trimmed = governance.trim_frame(frame, column="date")
    assert trimmed["date"].max() < pd.Timestamp(governance.FINAL_HOLDOUT_START)


def test_the_ledger_counts_every_configuration_and_sweeps_record_themselves() -> None:
    from src.research import sweep
    from test_signal_series_parity import _bars as random_bars

    before = governance.total_trials()
    governance.record_trials("manual", 10, family="trend")
    sweep({"X/USD": random_bars(1)}, "moving_average_crossover", grid={"short_window": [3, 5], "long_window": [20, 40]}, long_only=(False, True))
    assert governance.total_trials() == before + 10 + 8  # 4 combos x 2 sides
    assert governance.total_trials(family="trend") == 10 + 8 and governance.total_trials(include_backfill=False) == before - governance.BACKFILL_TRIALS + 18


def test_a_manifest_records_commit_arguments_and_holdout_state(tmp_path) -> None:
    path = governance.write_manifest(tmp_path / "run", args={"symbols": ["BTC/USD"]}, data="abc123")
    manifest = json.loads(path.read_text())
    assert manifest["data"] == "abc123" and manifest["args"] == {"symbols": "['BTC/USD']"}
    assert manifest["commit"] and manifest["final_holdout_unlocked"] == "tests"
