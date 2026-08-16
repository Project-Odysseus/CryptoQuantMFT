"""Helpers for writing a continuously updating runtime equity plot to disk."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


class RuntimeLivePlotter:
    """Persist a simple equity-and-trade plot while the runtime is running."""

    def __init__(self, output_path: str | Path | None = None) -> None:
        self.output_path = Path(output_path or "plots/runtime_live_plot.png")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._equity_series: list[float] = []
        self._timestamps: list[datetime] = []
        self._trade_prices: list[float] = []
        self._trade_timestamps: list[datetime] = []
        self._initial_equity: float | None = None

    def update(
        self,
        *,
        equity: float,
        timestamp: datetime,
        trades: Sequence[object] | None = None,
        initial_equity: float | None = None,
        title: str = "Runtime Equity",
    ) -> Path:
        """Append the latest cycle's equity and any new trades, then save the plot."""
        if initial_equity is not None:
            self._initial_equity = float(initial_equity)

        self._equity_series.append(float(equity))
        self._timestamps.append(timestamp)

        if trades:
            for trade in trades:
                trade_price = getattr(trade, "price", None)
                trade_timestamp = getattr(trade, "timestamp", None)
                if trade_price is None or trade_timestamp is None:
                    continue
                self._trade_prices.append(float(trade_price))
                self._trade_timestamps.append(trade_timestamp)

        self._render(title=title)
        return self.output_path

    def _render(self, *, title: str) -> None:
        fig, axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
        if len(self._timestamps) == 1:
            axes[0].plot([self._timestamps[0]], [self._equity_series[0]], color="#2563eb", marker="o", linewidth=1.8)
        else:
            axes[0].plot(self._timestamps, self._equity_series, color="#2563eb", linewidth=1.8)

        if self._initial_equity is not None:
            axes[0].axhline(self._initial_equity, color="#6b7280", linestyle="--", linewidth=1.0, alpha=0.7)

        axes[0].set_title(title)
        axes[0].set_ylabel("Equity")
        axes[0].grid(alpha=0.3)

        if self._trade_timestamps and self._trade_prices:
            axes[1].scatter(self._trade_timestamps, self._trade_prices, color="#ef4444", s=28, alpha=0.85)
            axes[1].plot(self._trade_timestamps, self._trade_prices, color="#111827", linewidth=1.0, alpha=0.7)
        else:
            axes[1].text(0.5, 0.5, "No trades yet", ha="center", va="center", transform=axes[1].transAxes, color="#6b7280")

        axes[1].set_xlabel("Time")
        axes[1].set_ylabel("Trade Price")
        axes[1].grid(alpha=0.3)
        fig.autofmt_xdate()
        fig.tight_layout()

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(self.output_path, dpi=150)
        plt.close(fig)
