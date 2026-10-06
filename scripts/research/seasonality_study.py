"""When does crypto move most? Epoch folding of hourly volatility for BTC and ETH, tested properly, and used in a forecast.

    python scripts/research/seasonality_study.py

From an Instagram reel on pulsar-style "epoch folding". Four questions, on Binance perp 5-minute bars (2021-03 to
2025-12-31; the frozen holdout stays locked). An hour's volatility is its realized variance: the sum of its twelve
squared 5-minute returns.

1. The profile: average volatility by hour of the day and of the week, relative to the average hour.
2. Does it move? The profile per year, and how alike consecutive years are.
3. Is it real? The fold statistic at the daily and weekly period, and at every trial period from 2 to 200 hours,
   against two nulls: single hours shuffled (the naive test) and blocks of about two weeks shuffled (keeps
   volatility clustering). Bonferroni over the periods searched.
4. Does it help a forecast? A one-hour-ahead forecast of volatility with and without the clock pattern, scored by
   QLIKE on every hour after the first year, per year.

It describes volatility, not direction: it can say when to expect larger moves, not which way. Logged as 6 looks
(family "volatility").
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import governance, pit, seasonal  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402

COINS = ("BTC", "ETH")
DAYS_OF_WEEK = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
BLOCK = 24 * 14


def hourly_variance(coin: str) -> pd.Series:
    """Realized variance per hour (indexed by the hour's start) from 5-minute closes; hours with missing bars are dropped."""
    close = pit.load_bars(coin, "5m")["close"]
    squared = np.log(close).diff() ** 2
    grouped = squared.groupby(squared.index.floor("h"))
    variance = grouped.sum()[grouped.count() == 12]
    return variance[variance > 0]


def main() -> None:
    out = Path("data/research") / f"seasonality_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    governance.write_manifest(out)
    lines = ["# Clock patterns in hourly volatility (BTC, ETH; 2021-03 to 2025-12)", ""]
    for coin in COINS:
        variance = hourly_variance(coin)
        vol = np.sqrt(variance)  # the profile is shown for volatility (the size of the move), the forecast is of variance
        index = variance.index
        day_phase, week_phase = seasonal.clock_phase(index, 24), seasonal.clock_phase(index, 168)
        by_hour = seasonal.fold(vol.to_numpy(), day_phase, 24) / vol.mean()
        by_week = seasonal.fold(vol.to_numpy(), week_phase, 168) / vol.mean()
        by_day = by_week.reshape(7, 24).mean(axis=1)
        order = np.argsort(by_hour)
        lines += [f"## {coin}", "", f"{len(variance):,} hours. Busiest hours (UTC): " + ", ".join(f"{hour:02d}:00 ({by_hour[hour]:.2f}x)" for hour in order[::-1][:4])
                  + ". Calmest: " + ", ".join(f"{hour:02d}:00 ({by_hour[hour]:.2f}x)" for hour in order[:4]) + ".",
                  "By day: " + ", ".join(f"{name} {value:.2f}x" for name, value in zip(DAYS_OF_WEEK, by_day)) + f". The hour from 00:00 UTC (when the books decide): {by_hour[0]:.2f}x.", ""]

        yearly = {}
        for year, part in vol.groupby(index.year):
            profile = seasonal.fold(part.to_numpy(), seasonal.clock_phase(part.index, 24), 24) / part.mean()
            yearly[year] = profile
        years = sorted(yearly)
        drift = pd.DataFrame({"busiest hours (UTC)": {year: ", ".join(f"{hour:02d}" for hour in np.argsort(yearly[year])[::-1][:3]) for year in years},
                              "peak / average": {year: float(yearly[year].max()) for year in years},
                              "00:00 hour": {year: float(yearly[year][0]) for year in years},
                              "13-15 UTC (US open)": {year: float(yearly[year][13:16].mean()) for year in years},
                              "correlation with the year before": {year: float(np.corrcoef(yearly[year], yearly[year - 1])[0, 1]) if year - 1 in yearly else np.nan for year in years}})
        lines += ["Per year:", "", md_table(drift, digits=2), ""]

        log_vol = np.log(vol).to_numpy()  # logs tame the heavy tail, so one wild hour doesn't decide the test
        rows = {}
        for label, period, phase in (("daily (24 h)", 24, day_phase), ("weekly (168 h)", 168, week_phase)):
            statistic = seasonal.fold_statistic(log_vol, phase, period)
            naive = seasonal.null_statistics(log_vol, phase, period, block=1, runs=300, seed=0)
            blocked = seasonal.null_statistics(log_vol, phase, period, block=BLOCK, runs=300, seed=0)
            rows[label] = {"statistic": statistic, "naive null, largest of 300": float(naive.max()), "block null, largest of 300": float(blocked.max())}
        lines += ["Is the pattern real? (1 = flat; the statistic against shuffles with no clock pattern)", "", md_table(pd.DataFrame(rows).T, digits=1), ""]

        periods = list(range(2, 201))
        level = 1.0 - 0.01 / len(periods)  # Bonferroni: 1% over all the periods tried
        flagged_naive, flagged_block, strongest = [], [], []
        for period in periods:
            phase = seasonal.clock_phase(index, period)
            statistic = seasonal.fold_statistic(log_vol, phase, period)
            naive = seasonal.null_statistics(log_vol, phase, period, block=1, runs=60, seed=period)
            blocked = seasonal.null_statistics(log_vol, phase, period, block=BLOCK, runs=60, seed=period)
            # with 60 shuffles the tail is estimated from the null's mean and spread (a normal approximation of a narrow distribution)
            z = 3.9  # about the 1 - 0.01/199 quantile of a normal
            if statistic > naive.mean() + z * naive.std():
                flagged_naive.append(period)
            if statistic > blocked.mean() + z * blocked.std():
                flagged_block.append(period)
            strongest.append((statistic / blocked.mean(), period))
        # A daily or weekly rhythm also shows at any trial period that shares a factor with 168 hours (it lines up again
        # every few blocks). Only a period with no common factor is evidence of a different cycle.
        coprime = [period for period in periods if math.gcd(period, 168) == 1]
        other_naive = [period for period in flagged_naive if period in coprime]
        other_block = [period for period in flagged_block if period in coprime]
        top = ", ".join(f"{period} h ({ratio:.0f}x)" for ratio, period in sorted(strongest, reverse=True)[:6])
        lines += [f"Trial periods 2 to 200 hours: the naive test flags {len(flagged_naive)} of {len(periods)}, the block test {len(flagged_block)}; strongest {top}. "
                  f"Of the {len(coprime)} periods that share no factor with a week (so can't be an echo of the daily or weekly rhythm), the naive test flags "
                  f"{len(other_naive)} and the block test {len(other_block)}" + (f" ({', '.join(str(period) for period in other_block[:10])} h)" if other_block else "") + ".", ""]

        plain = seasonal.ewma_forecast(variance, 24.0)
        models = {"no clock": plain, "hour of day": seasonal.seasonal_forecast(variance, 24), "hour of week": seasonal.seasonal_forecast(variance, 168)}
        measured = variance[variance.index >= variance.index[0] + pd.Timedelta(days=365)]
        scores = {}
        for year, part in measured.groupby(measured.index.year):
            scores[year] = {name: seasonal.qlike(forecast.reindex(part.index), part) for name, forecast in models.items()}
        scores["all"] = {name: seasonal.qlike(forecast.reindex(measured.index), measured) for name, forecast in models.items()}
        table = pd.DataFrame(scores).T
        table["hour of day, change"] = table["hour of day"] / table["no clock"] - 1.0
        table["hour of week, change"] = table["hour of week"] / table["no clock"] - 1.0
        lines += ["One-hour-ahead volatility forecast, QLIKE loss (lower is better; change against the forecast with no clock):", "", md_table(table, digits=3), ""]
        pd.DataFrame({"hour_of_week": range(168), "relative_volatility": by_week}).to_csv(out / f"profile_{coin}.csv", index=False)
        table.to_csv(out / f"forecast_{coin}.csv")
        print("\n".join(lines[-40:]) if coin == COINS[0] else "", flush=True)
    governance.record_trials("seasonality_study", 6, family="volatility", data="binance perp 5m, BTC and ETH, 2021-03 to 2025-12-31",
                             details={"note": "epoch folding of hourly realized variance; 2 seasonal forecasts x 2 coins, plus the period search"})
    (out / "report.md").write_text("\n".join(lines))
    print("\n".join(lines))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
