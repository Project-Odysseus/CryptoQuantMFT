"""Simple fee and FX cost modeling for backtests and paper trading."""

from __future__ import annotations

from dataclasses import dataclass

from src.data.fx import FXRateCollector


@dataclass(slots=True)
class CostModel:
    """Approximate trading costs for a single exchange and currency pair."""

    exchange: str
    maker_fee: float = 0.0
    taker_fee: float = 0.0
    fx_rate_collector: FXRateCollector | None = None
    fx_spread_bps: float = 10.0

    def apply_trade_cost(self, price: float, *, side: str, role: str = "taker", size: float = 1.0) -> float:
        """Return the net price after applying fees and FX spread."""
        fee_rate = self.maker_fee if role == "maker" else self.taker_fee
        fee_pct = fee_rate / 100.0
        fee_cost = price * fee_pct * size
        normalized_side = side.lower()
        if normalized_side in {"buy", "long"}:
            return price + fee_cost
        if normalized_side in {"sell", "short"}:
            return price - fee_cost
        return price + fee_cost

    def apply_fx_cost(self, price: float, *, side: str = "buy", size: float = 1.0) -> float:
        """Add a small FX spread cost for EUR/NOK conversion."""
        spread_rate = self.fx_spread_bps / 10000.0
        normalized_side = side.lower()
        if normalized_side in {"buy", "long"}:
            return price * (1 + spread_rate) * size
        if normalized_side in {"sell", "short"}:
            return price * (1 - spread_rate) * size
        return price * (1 + spread_rate) * size


def build_default_cost_model(exchange: str, *, fx_rate_collector: FXRateCollector | None = None) -> CostModel:
    """Create a simple first-pass cost model for the configured exchange."""
    fee_map = {
        "kraken": {"maker": 0.25, "taker": 0.40},
        "firi": {"maker": 0.10, "taker": 0.20},
        "mock": {"maker": 0.0, "taker": 0.0},
    }
    defaults = fee_map.get(exchange.lower(), {"maker": 0.0, "taker": 0.0})
    return CostModel(
        exchange=exchange,
        maker_fee=defaults["maker"],
        taker_fee=defaults["taker"],
        fx_rate_collector=fx_rate_collector,
        fx_spread_bps=10.0,
    )
