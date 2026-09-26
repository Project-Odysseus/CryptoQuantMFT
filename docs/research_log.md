# Research log

Dated findings from strategy research, newest first. Methodology and column meanings are in
[`research_guide.md`](research_guide.md). Record failures too, so they don't get re-tested by accident.

---

## 2026-09-26: Full history on BTC and ETH perpetuals, 2020-02-26 to 2026-09-25

**Data:** Kraken Futures trade candles, stitched from the inverse perpetuals (`PI_XBTUSD` / `PI_ETHUSD`, from
2020-02-26 to 2022) and the linear ones (`PF_`, from 2023-01-01), 2,404 daily and 14,422 4h bars each, no gaps.
Load with `load_bars("BTC/USD", "1d", source="perp")` or `--source perp`. *Correction:* the first version of this
entry used the inverse contracts for the whole period, but they went quiet after 2022 (by 2026, 42-69% of their 15m
bars had no trades). The numbers below are from the clean stitched series; they moved by a few points at most.
The series starts two weeks before the 12 March 2020 crash, so 4h and short-window strategies trade through its
tail while long-window daily ones are still warming up. Costs: perp fees (0.05% + 5 bps per fill) and funding
0.01%/day; Kraken only publishes the last year of funding, so 0.05%/day (roughly 2021 bull-market levels) was run
as a stress test. Full size, no leverage, no stops.

**Sweep** (`research.py sweep --source perp --symbols BTC/USD ETH/USD --intervals 1d 4h --venue perp`; in-sample
2020-06 to 2024-10, holdout 2024-10 to 2026-09; median Sharpe across combos, BTC/ETH averaged):

- **Daily trend-following is robust across every setting.** `moving_average_crossover`, `donchian_breakout` and
  `keltner_breakout` are positive in-sample for 100% of combos, long/short and long-only. Median in-sample / holdout
  Sharpe: long-only MA 1.10 / 0.75, Keltner 1.03 / 0.65, Donchian 0.99 / 0.45; long/short MA 0.75 / 0.68, Donchian
  0.72 / 0.64, Keltner 0.60 / 0.51. Buy-and-hold: 1.03 / 0.49. So daily trend roughly matched holding in the
  2020-2024 period and beat it in the choppier 2024-2026 holdout.
- **Mean reversion fails over the full history**, even at perp costs: `band_reversion` and `rsi_reversion` are
  negative for almost every combo long/short, and around 0.2 long-only. The positive 4h result in the entry below
  came from one 3-month rally.
- **4h trend is weaker than daily**: in-sample medians 0.2-0.44 long/short and 0.8-0.94 long-only, but holdout
  roughly flat, below buy-and-hold's 0.49.

**Year by year** (mid-plateau parameters, not the best cells; funding 0.01%/day; per-strategy start after warmup,
April 2020 for daily, March 2020 for 4h). CAGR / Sharpe / max drawdown vs buy-and-hold over the same dates:

| | BTC | ETH |
| --- | --- | --- |
| Buy and hold (daily, from 15 Apr 2020) | 48% / 0.97 / 77% | 56% / 0.96 / 79% |
| `keltner_breakout(40, 2)` 1d long-only | 42% / 1.15 / **39%** | 52% / 1.08 / **42%** |
| `moving_average_crossover(4, 48)` 1d long-only | 53% / 1.21 / 61% | 65% / 1.16 / 61% |
| `donchian_breakout(40, 10)` 1d long-only | 33% / 0.95 / 40% | 27% / 0.74 / 54% |
| `keltner_breakout(40, 2)` 1d long/short | 35% / 0.88 / 57% | 52% / 0.97 / 69% |
| `moving_average_crossover(8, 96)` 4h long-only | 42% / 1.07 / 51% | 44% / 0.94 / 54% |

- **The 2022 bear market is where trend earns its keep.** Buy-and-hold lost 64% (BTC) and 67% (ETH). Long-only
  Keltner lost 32% / 21%, long/short Keltner 23% / 9%, and long/short `trend_tstat` made +15% / +52%.
- **The cost is lagging in strong bulls.** From 7 April to the end of 2020 buy-and-hold made +303% / +348% against
  +239% / +195% for long-only Keltner, and in 2023-24 BTC buy-and-hold beat most rules.
- **2025-2026 was choppy.** Buy-and-hold was -6% / -11% in 2025, and most trend rules were near flat, with ETH
  long/short MA crossover (+126%) and Keltner (+98%) the exceptions.
- **Long-only beats long/short on BTC** in every row and on ETH in most (`trend_tstat` is the exception). Shorts pay
  off in 2022 and cost in bull years.
- **Costs are small here**: daily rules trade 4-12 times a year. The 0.05%/day funding stress cut long-only CAGR by
  7-12 points (longs pay funding) and long/short by 0-5 (shorts receive it); everything stayed positive.
- **Drawdowns are still large at full size** (39-78%). Position sizing, not a better signal, is what would make
  these tradable with real money.

**Caveats.** The strategy families were picked from earlier research on 2025-26 data and the parameters here are
plateau centres chosen on this same history, so this is a robustness check, not a clean out-of-sample test. BTC
and ETH move together, so two coins are not two independent confirmations. 2020-21 dominates the compounding.
Funding is a flat assumption before 2025. The inverse-contract prices stand in for the linear contracts.

---

## 2026-09-25 (latest): Perp assumptions checked against Kraken Futures' public data

Checked with `python main.py --futures-venue-check --futures-symbol BTC/USD` (public endpoints, no credentials):

- **Fees confirmed:** `PF_XBTUSD` uses the schedule with 0.02% maker and 0.05% taker at the entry tier (0 USD of
  30-day volume), exactly the assumption below. Slippage (5 bps) is still an assumption.
- **Funding is about a third of what I assumed.** Hourly funding over the last year, in percent of notional per
  day (positive = longs pay): BTC mean 0.0087, median 0.0084, p10 -0.014, p90 0.032, min -0.43, max 0.15; ETH mean
  0.0086 (min -1.3); SOL mean -0.001 (min -3.0, max +0.65). 31-32% of hours are negative for BTC and ETH, 48% for
  SOL, and BTC funding was negative Feb-Apr 2026. `CostSettings.perp()` now defaults to 0.01%/day. Treat 0.03 as a
  stress case and 0.10 as extreme.
- **Contract facts:** USD-quoted linear flexible futures; size steps 0.0001 BTC / 0.001 ETH / 0.01 SOL; first-tier
  margin 1% initial (venue limit 100x) and 0.5% maintenance, with 8 tiers rising by position size.
- **Rerun with measured funding** (`--venue perp`, median Sharpe across parameter combos, in-sample / holdout,
  share of combos positive in-sample in brackets). 4h long-only: `keltner_breakout` 1.69 / 2.17 (100%),
  `moving_average_crossover` 1.48 / 2.60 (100%), `donchian_breakout` 1.32 / 1.93 (100%), `band_reversion`
  1.41 / 2.11 (92%), `rsi_reversion` 1.27 / 0.25 (100%). Daily long/short: `moving_average_crossover` 0.79 / 0.15
  (100%), `keltner_breakout` 0.59 / 0.67 (92%), `donchian_breakout` 0.25 / 0.35 (100%). The conclusions are the
  same as below and the improvement is a bit larger; 4h buy-and-hold (2.21 / 4.64) is still above every long-only row.

## 2026-09-25 (later): Same sweep under perpetual-futures costs

**Assumptions used for this table:** 0.05% taker + 5 bps slippage per fill (~0.2% round trip), 0.02% maker, funding
0.03%/day (stress: 0.10%/day) charged on the position, positive = longs pay. The fees were later confirmed and the
funding found to be lower (see the entry above); reproduce with `--venue perp --funding-pct-per-day 0.03`. Median Sharpe across all parameter combos,
in-sample / holdout:

| 4h, long-only | Spot taker | Perp taker, no funding | Perp taker + 0.03%/day | Perp taker + 0.10%/day | Perp maker + 0.03%/day |
| --- | --- | --- | --- | --- | --- |
| keltner_breakout | 0.29 / 1.24 | 1.71 / 2.23 | 1.64 / 2.07 | 1.38 / 1.71 | 1.94 / 2.27 |
| moving_average_crossover | 0.39 / 1.47 | 1.54 / 2.66 | 1.37 / 2.47 | 0.96 / 2.02 | 1.62 / 2.69 |
| band_reversion | -4.36 / -6.78 | 1.43 / 2.15 | 1.38 / 2.01 | 1.28 / 1.68 | 2.61 / 3.23 |
| rsi_reversion | -4.15 / -6.55 | 1.32 / 0.31 | 1.17 / 0.13 | 0.97 / -0.18 | 2.57 / 0.96 |

- **Lower costs revive mean reversion and speed up everything else.** Costs, not signal quality, were what made
  `band_reversion` and `rsi_reversion` lose on spot. At ~0.2% per round trip they turn positive.
- **Funding matters less than fees at 0.03%/day** (about 0.1-0.2 Sharpe) but not at 0.10%/day (0.3-0.6). Funding
  is a moving target and should be measured from the venue's history, not assumed.
- **Real shorting helps.** 4h long/short goes from negative on every strategy on spot to about break-even, with
  `moving_average_crossover` positive in both periods (0.43 / 0.19 with 0.03%/day funding). Daily long/short
  `keltner_breakout` improves from 0.30 / 0.44 to 0.59 / 0.65.
- **Still not better than holding in a rally.** 4h buy-and-hold Sharpe was 2.21 in-sample and 4.64 in the
  holdout, above almost every long-only row. The 4h evidence is one 3-month rally, so the mean-reversion result
  in particular needs more history before it means anything.
- Sharpe ignores leverage and liquidation risk, which spot does not have.

---

## 2026-09-25: Sensitivity sweep of 10 strategies (6 new), BTC/ETH/SOL vs EUR, 4h and 1d

**Run:** `python scripts/research/research.py sweep --intervals 4h 1d`, repeated at three cost levels:
taker (0.40% + 10 bps per fill, ~1.0% round trip, the default), maker (0.25%, no slippage, ~0.5%) and zero.
126 parameter combos × long-only and long/short × 3 symbols × 2 intervals = 1,512 runs per cost level. Outputs
were in `data/research/sweep_20260925_{full,maker,zero_cost}/`, which is gitignored, so re-run the command to
regenerate them.

**Periods** (holdout = last 30%):

| Interval | In-sample | Holdout | Buy-and-hold Sharpe IS / HO (mean of 3) |
| --- | --- | --- | --- |
| 4h | 2026-06-15 → 2026-08-21 (~2 months) | 2026-08-21 → 2026-09-25 (~5 weeks) | 2.21 / 4.64 (strong rally) |
| 1d | 2025-01-17 → 2026-02-21 (~13 months) | 2026-02-22 → 2026-09-25 (~7 months) | −0.63 / 1.33 (bear, then rally) |

On daily bars, buy-and-hold lost 40% / 48% / 65% (BTC/ETH/SOL) in-sample, with max drawdowns of 50–74%, then
gained 27% / 41% / 47% in the holdout.

### Findings

1. **Trading costs decide almost everything.** The median gross profit per trade is under 0.5% for most families
   and under ~0.7% for all but one (daily long/short MA crossover, 1.2%), against a ~1.0% round-trip cost. Every family's median Sharpe climbs steeply from taker → maker → zero cost.
   For 4h long-only `keltner_breakout` it goes 0.29 → 1.19 → 2.12, and for 4h long-only `band_reversion` it goes
   −4.36 → −0.58 → +3.10.

2. **Mean reversion and short-horizon momentum are not tradable at our fee tier.** `band_reversion`,
   `rsi_reversion`, `trend_pullback`, `momentum_breakout` and `volume_confirmed_momentum` have a negative median
   Sharpe net of costs on both intervals and both sides, with 0–40% of combos positive (usually under 25%). On 4h long-only, mean reversion has
   real *gross* edge (`band_reversion` gross median Sharpe 3.10 IS / 3.56 HO). But its average gross trade (~0.4%)
   is under half the round-trip cost, so it stays negative even with maker orders. Revisit only if costs fall a
   lot (fee tier) or with a version that makes much bigger moves per trade.

3. **Slow trend-following is the only family that is robust net of costs.**
   - 4h long-only: `donchian_breakout` (82% of combos positive, median Sharpe 0.62 IS / 1.20 HO),
     `moving_average_crossover` (75%, 0.39 / 1.47), `keltner_breakout` (67%, 0.29 / 1.24), `trend_tstat`
     (62%, 0.28 / 1.23). The MA crossover heatmap shows a clean plateau along `long_window=96`: IS 0.95–1.46 and
     HO 2.81–3.28 for every short window.
   - 1d long/short: `moving_average_crossover` (88%, 0.59 / −0.29), `keltner_breakout` (83%, 0.30 / 0.44) and
     `donchian_breakout` (64%, 0.07 / 0.10). For `keltner_breakout`, the `window=40` column is positive at every ATR
     multiplier in both periods (IS 0.55–0.99, HO 0.42–0.79). On daily bars, Keltner and (weakly) Donchian
     long/short, plus long-only `volatility_squeeze` (0.12 / 0.03), are the families that stay positive in both IS
     and HO at all three cost levels, as does the whole 4h long-only trend group above.
   - **Slower is better** almost everywhere: longer windows mean fewer trades and less noise. The
     `moving_average_crossover(3,6)` on 1-second bars used in the first live test is at the far bad end of that
     axis.

4. **Nothing beats buy-and-hold in a rising market. Trend-following earns its keep in falling ones.**
   - 4h long-only MA crossover (8, 96): Sharpe 0.76–2.47 vs buy-and-hold 1.73–2.92 in-sample, 1.75–5.19 vs
     3.79–5.90 in holdout, below holding on every symbol in both periods. That's partial exposure to a rally.
   - 1d `keltner_breakout(40, 1.0)` long/short, 2025 bear market: +13% / +134% / +44% vs buy-and-hold
     −40% / −48% / −65%, with max drawdowns of 35–54% vs 50–74%. In the 2026 rally: −8% / +32% / +2% vs
     +27% / +41% / +47%.
   - This is the classic trend-following profile. It's a way to be out of (or short) bear markets, not an edge
     over holding in bull markets. Judge it on drawdown avoided across a full cycle, not on bull-market Sharpe.

5. **Long-only vs long/short flips with the regime.** In the 4h rally sample, long/short is negative for every
   family (net median Sharpe ≤ −0.98) while long-only trend is positive. In the 1d bear in-sample, long/short trend
   beats long-only. Spot live trading is effectively long-only, so the long-only rows are what is tradable today.

6. **Don't pick the best cell.** 4h long-only `trend_tstat`: best IS 1.91 → HO −4.59 (neighbours 0.77, rank corr
   −0.51). 4h long-only `donchian_breakout`: best IS 1.59 → HO −0.83, while its median combo did +1.20 in HO. Pick
   from the middle of a plateau.

7. **`rank_corr` is inflated by trade count in losing families.** Within `rsi_reversion` on 4h, IS Sharpe
   correlates −0.97 with the number of trades. The combos that trade least lose least in both periods, which looks
   like "IS ranking carries over" without any signal behind it.

### Candidates worth taking further (not live yet)

- `keltner_breakout`, `window≈40`, `atr_multiplier≈1.5` (centre of the plateau) on **daily** bars, long-only on
  spot. It works as a bear-market filter.
- `moving_average_crossover`, `short_window 4–8`, `long_window 96` on **4h** bars, long-only.

Caveats: the 4h evidence is 3 months of one rally. The daily evidence is one bear market followed by one rally.
BTC/ETH/SOL are highly correlated, and daily long/short drawdowns of 35–54% at full size would need sizing down.

### What this points to next

0. ~~Blocker: runtime treated signal 0 as "hold" while research treats it as "flat"~~ Fixed 2026-09-25, the
   runtime now closes on 0.
1. **More history.** Load Kraken's OHLCVT CSVs (see the guide) and re-run this sweep over several cycles. It is the
   biggest single improvement to confidence, and cheap.
2. **Limit (maker) orders for execution.** Cutting the round trip from ~1.0% to ~0.5% roughly doubles the 4h trend
   family's Sharpe. At our trade size this matters far more than order-book depth.
3. **Runtime bridge.** The runtime needs a 4h/1d bar interval and historical warmup seeding before either candidate
   can be paper traded.
4. **Stops.** Fix re-entry after a forced exit before researching stop-losses.
