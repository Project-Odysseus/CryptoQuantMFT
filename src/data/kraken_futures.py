"""Read-only client for Kraken Futures' public REST endpoints.

Nothing here needs credentials and nothing places or changes an order. It
covers what research and the sandbox need to be grounded in the real venue:
instrument specifications (size precision, tick size, margin tiers), fee
schedules, live tickers (mark and index price, current funding) and the
hourly funding-rate history.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.data.historical import _request_json

PUBLIC_BASE = "https://futures.kraken.com/derivatives/api"

# The runtime's "BASE/QUOTE" symbols map to Kraken's perpetual contract names.
# Kraken uses XBT for bitcoin. These are USD-quoted linear "flexible futures".
PERP_VENUE_SYMBOLS: dict[str, str] = {
    "BTC/USD": "PF_XBTUSD",
    "ETH/USD": "PF_ETHUSD",
    "SOL/USD": "PF_SOLUSD",
}


def venue_symbol_for(symbol: str) -> str:
    """Return Kraken's perpetual contract name for a runtime symbol such as ``BTC/USD``."""
    normalized = symbol.strip().upper()
    if normalized not in PERP_VENUE_SYMBOLS:
        raise ValueError(f"no Kraken perpetual is mapped for {symbol!r}; known: {sorted(PERP_VENUE_SYMBOLS)}")
    return PERP_VENUE_SYMBOLS[normalized]


@dataclass(frozen=True, slots=True)
class FundingRate:
    """One funding period: the relative rate charged per hour on position notional."""

    timestamp: datetime
    hourly_rate: float

    @property
    def pct_per_day(self) -> float:
        """The hourly rate expressed as a percentage of notional per day."""
        return self.hourly_rate * 24.0 * 100.0


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _request_json("GET", f"{PUBLIC_BASE}{path}", params=params)
    if not isinstance(payload, dict) or payload.get("result") != "success":
        raise RuntimeError(f"Kraken Futures request {path} failed: {str(payload)[:200]}")
    return payload


def fetch_instruments() -> list[dict[str, Any]]:
    """All listed instruments with their specifications (tick size, size precision, margin levels, fee schedule id)."""
    return list(_get("/v3/instruments").get("instruments", []))


def fetch_instrument(venue_symbol: str) -> dict[str, Any]:
    """The specification of a single instrument, e.g. ``PF_XBTUSD``."""
    for instrument in fetch_instruments():
        if instrument.get("symbol") == venue_symbol:
            return instrument
    raise ValueError(f"instrument {venue_symbol} is not listed on Kraken Futures")


def fetch_fee_schedules() -> dict[str, dict[str, Any]]:
    """Fee schedules keyed by uid; each has volume tiers of maker and taker fees in percent."""
    return {schedule["uid"]: schedule for schedule in _get("/v3/feeschedules").get("feeSchedules", [])}


def fetch_tickers() -> dict[str, dict[str, Any]]:
    """Live tickers keyed by symbol, including markPrice, indexPrice, bid, ask and the current funding rate."""
    return {ticker["symbol"]: ticker for ticker in _get("/v3/tickers").get("tickers", [])}


def fetch_funding_history(venue_symbol: str) -> list[FundingRate]:
    """Roughly the last year of hourly funding rates, oldest first."""
    rows = _get("/v4/historicalfundingrates", {"symbol": venue_symbol}).get("rates", [])
    rates = [
        FundingRate(timestamp=datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")), hourly_rate=float(row["relativeFundingRate"]))
        for row in rows
    ]
    return sorted(rates, key=lambda rate: rate.timestamp)


def summarize_funding(rates: list[FundingRate]) -> dict[str, float]:
    """Distribution of funding as percent of notional per day (positive = longs pay).

    Returns mean, median, 10th/90th percentile, the share of hours with
    negative funding, the mean annualised, and the extremes. Funding is
    not steady: use the spread, not just the mean, when judging a carry cost.
    """
    if not rates:
        raise ValueError("no funding history to summarise")
    daily = sorted(rate.pct_per_day for rate in rates)

    def quantile(fraction: float) -> float:
        return daily[min(len(daily) - 1, int(fraction * len(daily)))]

    mean = statistics.fmean(daily)
    return {
        "hours": float(len(daily)),
        "mean_pct_per_day": mean,
        "median_pct_per_day": statistics.median(daily),
        "p10_pct_per_day": quantile(0.10),
        "p90_pct_per_day": quantile(0.90),
        "share_negative": sum(1 for value in daily if value < 0.0) / len(daily),
        "mean_pct_per_year": mean * 365.0,
        "min_pct_per_day": daily[0],
        "max_pct_per_day": daily[-1],
    }


CANDLE_RESOLUTIONS: dict[int, str] = {60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h", 14400: "4h", 43200: "12h", 86400: "1d", 604800: "1w"}
CHARTS_BASE = "https://futures.kraken.com/api/charts/v1"


def fetch_candles(
    venue_symbol: str,
    *,
    interval_seconds: int,
    count: int,
    symbol: str | None = None,
    tick_type: str = "mark",
    now: datetime | None = None,
) -> list[Any]:
    """The last `count` completed candles for a perpetual, oldest first, as OHLCVBar.

    `tick_type="mark"` matches what `KrakenFuturesConnector` builds live bars
    from (the mark price), so warmup and live bars are the same series. Mark
    candles carry no volume (Kraken reports 0); use `tick_type="trade"` when
    volume matters. The still-open candle is dropped. Kraken returns up to
    2000 candles per request, so this pages backwards as needed.
    """
    from datetime import timezone

    from src.storage.bar_aggregator import OHLCVBar

    if interval_seconds not in CANDLE_RESOLUTIONS:
        raise ValueError(f"Kraken Futures has no {interval_seconds}s candles; use one of {sorted(CANDLE_RESOLUTIONS)}")
    resolution = CANDLE_RESOLUTIONS[interval_seconds]
    end = int((now or datetime.now(timezone.utc)).timestamp())
    collected: dict[int, dict[str, Any]] = {}
    to_ts = end
    for _ in range(10):
        from_ts = to_ts - interval_seconds * min(count + 2, 2000)
        payload = _request_json("GET", f"{CHARTS_BASE}/{tick_type}/{venue_symbol}/{resolution}", params={"from": from_ts, "to": to_ts})
        candles = payload.get("candles", []) if isinstance(payload, dict) else []
        for candle in candles:
            collected[int(candle["time"])] = candle
        if len(collected) >= count + 1 or not candles:
            break
        to_ts = from_ts
    completed = [candle for time_ms, candle in collected.items() if time_ms // 1000 + interval_seconds <= end]
    completed.sort(key=lambda candle: int(candle["time"]))
    return [
        OHLCVBar(
            exchange="kraken_futures",
            symbol=symbol or venue_symbol,
            interval_seconds=interval_seconds,
            timestamp=datetime.fromtimestamp(int(candle["time"]) // 1000, tz=timezone.utc),
            open=float(candle["open"]),
            high=float(candle["high"]),
            low=float(candle["low"]),
            close=float(candle["close"]),
            volume=float(candle.get("volume") or 0.0),
        )
        for candle in completed[-count:]
    ]
