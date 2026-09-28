"""Per-venue trading costs for the hypothesis studies: fees, spread and slippage that widen with volatility.

A fill pays the venue fee plus half the spread plus impact. Spread and impact are not constant: books thin out when
markets move, which is exactly when event strategies trade. So the non-fee part scales with the ratio of current
volatility to its normal level (a trailing one-year median, known at decision time), clipped to [0.5, 5].
Funding on perp legs is not a cost estimate but a cash flow, so it is charged from the actual settlements in
`bar_engine`, not here.

Fees are each venue's published entry tier (no VIP discounts, no BNB rebates). Spread and slippage are
assumptions, set a little above what BTC and ETH books show at small size; SOL's are doubled. Every study reports
gross, net, and net at 2x these costs (`multiplier=2`), which is the check that matters when an assumption is off.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

BPS = 1e-4
VOL_RATIO_CLIP = (0.5, 5.0)
COIN_SPREAD_FACTOR = {"BTC": 1.0, "ETH": 1.0, "SOL": 2.0}


@dataclass(frozen=True, slots=True)
class VenueCosts:
    """One venue's fees (fractions of notional per side) and calm-market spread and impact in bps per side."""

    name: str
    taker_fee: float
    maker_fee: float
    half_spread_bps: float
    slippage_bps: float

    def per_side(self, vol_ratio: float | np.ndarray | pd.Series = 1.0, *, coin: str = "BTC", taker: bool = True,
                 multiplier: float = 1.0, spread_stress: float = 1.0) -> float | np.ndarray | pd.Series:
        """Cost of one fill as a fraction of notional.

        Args:
            vol_ratio: Current volatility over its normal level (1 = a calm market). Clipped to [0.5, 5].
            coin: Scales spread and impact (SOL's books are thinner).
            taker: Taker fee if True, maker fee otherwise.
            multiplier: Scales the whole cost (2 = the doubled-cost robustness run).
            spread_stress: Scales only the spread (3 = the stressed spread H2 assumes when trading turbulence).
        """
        ratio = np.clip(vol_ratio, *VOL_RATIO_CLIP)
        spread = (self.half_spread_bps * spread_stress + self.slippage_bps) * BPS * COIN_SPREAD_FACTOR.get(coin, 2.0)
        fee = self.taker_fee if taker else self.maker_fee
        return (fee + spread * ratio) * multiplier

    def scaled(self, multiplier: float) -> "VenueCosts":
        """The same venue with every cost multiplied."""
        return replace(self, taker_fee=self.taker_fee * multiplier, maker_fee=self.maker_fee * multiplier,
                       half_spread_bps=self.half_spread_bps * multiplier, slippage_bps=self.slippage_bps * multiplier)


VENUES: dict[str, VenueCosts] = {
    # Kraken Futures entry tier: 0.02% maker / 0.05% taker. What the live book trades.
    "kraken_perp": VenueCosts("kraken_perp", taker_fee=0.0005, maker_fee=0.0002, half_spread_bps=1.0, slippage_bps=3.0),
    # Kraken spot entry tier: 0.25% maker / 0.40% taker.
    "kraken_spot": VenueCosts("kraken_spot", taker_fee=0.0040, maker_fee=0.0025, half_spread_bps=1.0, slippage_bps=3.0),
    # Binance USD-M regular tier: 0.02% / 0.05%; spot 0.10% / 0.10%.
    "binance_perp": VenueCosts("binance_perp", taker_fee=0.0005, maker_fee=0.0002, half_spread_bps=0.5, slippage_bps=1.5),
    "binance_spot": VenueCosts("binance_spot", taker_fee=0.0010, maker_fee=0.0010, half_spread_bps=0.5, slippage_bps=1.5),
    # Bybit linear regular tier: 0.02% / 0.055%.
    "bybit_perp": VenueCosts("bybit_perp", taker_fee=0.00055, maker_fee=0.0002, half_spread_bps=0.5, slippage_bps=1.5),
}


def vol_ratio(close: pd.Series, *, window: int, normal_window: int) -> pd.Series:
    """Trailing realized vol over its trailing median (both known at each bar's close); 1.0 until there is history.

    Args:
        close: Closes at a fixed bar interval.
        window: Bars in the current-volatility window (e.g. 24 for a day of hourly bars).
        normal_window: Bars in the median defining "normal" (e.g. 365 days of bars).
    """
    returns = np.log(close).diff()
    current = returns.rolling(window, min_periods=max(2, window // 2)).std()
    normal = current.rolling(normal_window, min_periods=max(2, window)).median()
    return (current / normal).fillna(1.0).clip(*VOL_RATIO_CLIP)
