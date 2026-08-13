from __future__ import annotations

from datetime import datetime, timezone

from src.backtest.walk_forward import evaluate_walk_forward
from src.storage.bar_aggregator import OHLCVBar


def test_evaluate_walk_forward_returns_fold_summaries() -> None:
    bars = []
    for index in range(20):
        close = 100.0 + index * 0.5
        bars.append(
            OHLCVBar(
                exchange="mock",
                symbol="BTC/NOK",
                interval_seconds=60,
                timestamp=datetime(2024, 1, 1, 0, index, tzinfo=timezone.utc),
                open=close - 0.1,
                high=close + 0.2,
                low=close - 0.2,
                close=close,
                volume=1.0,
            )
        )

    result = evaluate_walk_forward(bars, train_window=5, test_window=3, step_size=2)

    assert result.summary["fold_count"] == 7
    assert result.summary["avg_return"] is not None
    assert len(result.folds) == 7
    assert result.folds[0].return_pct != 0.0
