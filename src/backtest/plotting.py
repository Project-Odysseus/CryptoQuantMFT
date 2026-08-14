"""Reusable plotting helpers for equity curves and trade annotations."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


class StrategyPlotter:
    """Create simple matplotlib charts from backtest-style data."""

    def __init__(self, output_dir: str | Path | None = None) -> None:
        """Initialize the object with its runtime state."""
        self.output_dir = Path(output_dir or "plots")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def plot_equity_curve(
        self,
        equity_series: Sequence[float],
        *,
        title: str = "Equity Curve",
        timestamps: Sequence[datetime] | None = None,
        output_path: str | Path | None = None,
    ) -> Path:
        """Plot a simple equity curve line chart against time when timestamps are available."""
        fig, ax = plt.subplots(figsize=(8, 4))
        values = list(equity_series)
        if timestamps is not None and len(timestamps) == len(values):
            ax.plot(list(timestamps), values, color="#2563eb", linewidth=1.8)
            ax.set_xlabel("Time")
        else:
            ax.plot(values, color="#2563eb", linewidth=1.8)
            ax.set_xlabel("Bar Index")

        ax.set_title(title)
        ax.set_ylabel("Equity")
        ax.grid(alpha=0.3)
        fig.tight_layout()

        target_path = Path(output_path or self.output_dir / f"{title.lower().replace(' ', '_')}.png")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(target_path, dpi=150)
        plt.close(fig)
        return target_path

    def plot_trades(
        self,
        trade_prices: Sequence[float],
        *,
        title: str = "Trades",
        trade_timestamps: Sequence[datetime] | None = None,
        output_path: str | Path | None = None,
    ) -> Path:
        """Plot trade entries as points over time when timestamps are available."""
        if not trade_prices:
            raise ValueError("At least one trade price is required")

        fig, ax = plt.subplots(figsize=(8, 4))
        prices = list(trade_prices)
        if trade_timestamps is not None and len(trade_timestamps) == len(prices):
            ax.plot(list(trade_timestamps), prices, color="#111827", linewidth=1.4)
            ax.scatter(list(trade_timestamps), prices, color="#ef4444", s=20, alpha=0.8)
            ax.set_xlabel("Time")
        else:
            ax.plot(prices, color="#111827", linewidth=1.4)
            ax.scatter(range(len(prices)), prices, color="#ef4444", s=20, alpha=0.8)
            ax.set_xlabel("Trade Index")

        ax.set_title(title)
        ax.set_ylabel("Price")
        ax.grid(alpha=0.3)
        fig.tight_layout()

        target_path = Path(output_path or self.output_dir / f"{title.lower().replace(' ', '_')}.png")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(target_path, dpi=150)
        plt.close(fig)
        return target_path

    def plot_equity_and_trades(
        self,
        equity_series: Sequence[float],
        trade_prices: Sequence[float],
        *,
        title: str = "Equity and Trades",
        timestamps: Sequence[datetime] | None = None,
        trade_timestamps: Sequence[datetime] | None = None,
        price_series: Sequence[float] | None = None,
        price_timestamps: Sequence[datetime] | None = None,
        output_path: str | Path | None = None,
    ) -> Path:
        """Create a combined chart with equity and price/trade markers over time."""
        fig, axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
        values = list(equity_series)
        if timestamps is not None and len(timestamps) == len(values):
            axes[0].plot(list(timestamps), values, color="#2563eb", linewidth=1.8)
            axes[0].set_xlabel("Time")
        else:
            axes[0].plot(values, color="#2563eb", linewidth=1.8)
            axes[0].set_xlabel("Bar Index")
        axes[0].set_title(title)
        axes[0].set_ylabel("Equity")
        axes[0].grid(alpha=0.3)

        if price_series is not None and price_timestamps is not None and len(price_series) == len(price_timestamps):
            prices = list(price_series)
            axes[1].plot(list(price_timestamps), prices, color="#111827", linewidth=1.4, label="Price")
            if trade_timestamps is not None and len(trade_timestamps) == len(trade_prices):
                axes[1].scatter(list(trade_timestamps), list(trade_prices), color="#ef4444", s=20, alpha=0.8, label="Trades")
            axes[1].set_xlabel("Time")
        else:
            prices = list(trade_prices)
            axes[1].plot(prices, color="#111827", linewidth=1.4)
            axes[1].scatter(range(len(prices)), prices, color="#ef4444", s=20, alpha=0.8)
            axes[1].set_xlabel("Trade Index")

        axes[1].set_ylabel("Price")
        axes[1].grid(alpha=0.3)
        if price_series is not None and price_timestamps is not None:
            axes[1].legend(loc="best")

        fig.tight_layout()
        target_path = Path(output_path or self.output_dir / f"{title.lower().replace(' ', '_')}.png")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(target_path, dpi=150)
        plt.close(fig)
        return target_path
