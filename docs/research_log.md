# Research log

Dated findings from strategy research, newest first (entries from the same day too). Methodology and column
meanings are in [`research_guide.md`](research_guide.md). Record failures too, so they don't get re-tested by
accident.

---

## 2026-10-03: H4 and H5 pre-registered (Kalshi daily BTC markets against Deribit options); pilot only

Two hypotheses on one panel (`src/research/hypotheses/binary_panel.py`): at every recorded Deribit chain snapshot,
Kalshi's quotes on its daily "BTC above K at 5 PM New York" levels (within 3% of the index, 1-26 hours left) next to
the probability the option smile implies, and what happened. Pre-registered and locked before any result
(`research/prereg/H4.txt`, `H5.txt`; `python scripts/research/new_hypothesis.py status`).

- **H5:** where Deribit-implied and Kalshi's mid differ, the options are partly right (slope b > 0 in
  outcome - mid = b x (implied - mid)), and a taker rule on gaps above 2 cents after the fee is profitable.
- **H4:** downside protection bought as "No" contracts 0.5-2% below the price, at the ask plus the fee, loses money on
  average (the practical question: what does capping a perp long's loss this way cost?).

**The test is prospective.** The confirmatory window is 2026-10-05 to 2026-11-16, data that didn't exist when the
texts were locked, run once on or after 2026-11-17 (`--window confirm`; the scripts refuse earlier). At least 20
daily events are needed. `--collect` caches the window's Kalshi quotes as chains are recorded and prints counts only.

**Pilot** (the week already recorded: 54 snapshots, 7 daily events, 967 rows; a pipeline check, 3 trials logged). No
conclusion is drawn from it and no rule was changed after it:

| | Pilot value (95% interval over events) |
| --- | --- |
| H5 slope b | +1.58 (+0.65 to +2.50); flat-volatility version +0.95 (+0.22 to +1.72) |
| H5 Brier: Kalshi mid / options-implied | 0.0569 / 0.0551 |
| H5 taker rule | 239 trades in 6 events, +0.056 per contract (+0.013 to +0.107) |
| H4 protection: profit per contract | -0.082 (-0.126 to -0.043); cost 0.125, breach rate 0.043, options-implied fair 0.088 |
| H4 cost of covering a long down to the level | 12.9 bps of the position |

- The pipeline works end to end, and the gap has the opposite shape to the one predicted: Kalshi's quotes were *less*
  extreme than the options' (0.949 against 0.970 for levels more than 1% below the price; 0.053 against 0.030 above),
  and the outcomes were more extreme than both. The pre-registration expected the options' distribution to be the
  wider one. It stays as written; the confirmatory window decides.
- Seven events in one calm week: BTC settled below a protected level on 2 of them. Both pilot numbers are what a quiet
  week produces whatever the truth is. A premium for selling protection shows up exactly like this until the week
  it doesn't.
- The data depend on the option-chain recorder, which only runs while the laptop is awake (2 to 15 snapshots a day so
  far). An always-on machine would roughly triple the confirmatory sample.

---

## 2026-10-03: Prediction markets on BTC (Polymarket, Kalshi): a fair-value model and a first look. Research only

Both venues list binary markets on the BTC price: above a level at a time, inside a range, and up or down over a
window (15 minutes to a day). Their price is a probability, so each is a binary option. New read-only tools
(`src/research/prediction_markets.py`, `notebooks/prediction_markets.ipynb`; public data, no keys, no orders): both
venues' markets in one shape, fair value N(d2) with a recent-volatility or Deribit-implied sigma, edges after each
venue's taker fee (0.07 x p x (1 - p) per contract on both), the perp hedge (the binary's delta), a Deribit
call-spread cross-check, cross-venue pairs, and calibration tests. 7 configurations logged (family
`prediction_markets`).

**The model against history** (Binance 5m BTC perp, 2021-03 to 2025-12; every 15-minute and 1-hour window as an
up/down question, priced partway through from the move so far and an EWMA volatility of past returns):

| | Forecasts | Brier (coin flip 0.25) | Said 60-70%, happened | Said 30-40%, happened |
| --- | --- | --- | --- | --- |
| 15-minute windows | 339,070 | 0.173 | 71.5% | 28.9% |
| 1-hour windows | 466,213 | 0.165 | 71.7% | 29.0% |

- The model has information, about the same every year (Brier 0.171-0.174), and it is **underconfident in the
  middle**: the volatility estimate is an average pulled up by violent bars, so an ordinary window moves less than
  assumed. Scaling volatility by ~0.85 gives the best log loss on the last 120,000 bars.
- **Against Kalshi's own quotes** (30 settled 15-minute markets, 420 market-minutes, today only): the market's
  midpoint was closer to the outcomes than the model (Brier 0.124 against 0.142), with spreads of 0.3 to 1 cent.
  The model sees Binance 1-minute closes; the market sees the settlement index (CF Benchmarks' BRTI) and the flow.
- Kalshi's 15-minute BTC markets traded 2-3 million contracts each today; Polymarket's a few thousand USD. Same-level,
  same-hour pairs across the venues exist (hourly "above" markets); today none was locked after fees, and the venues
  settle on different price sources, so a small positive pair is basis risk.

**What this changes:** nothing is tradable from this. The naive model does not beat the market it would trade
against, as expected. The tools are there for properly pre-registered questions (notebook, section 9). Whether these
venues may be used at all (employer rules, availability in Norway) is unchecked and comes before any of it.

---

## 2026-10-03: The taker-buy basket, side by side: do the shorts earn their place?

Question from the owner: the basket shorts the coins with the lowest taker-buy share, but is there evidence those
coins lag, or does the long side do all the work? The basket exactly as configured (top 30, 20% legs, every 10 days;
nothing changed), on Kraken perp prices, each side measured against the equal-weight ranked universe held over the
same rebalance days. `python scripts/research/basket_legs.py`; 1 look logged (family `cross_sectional`). Annualised %
per 1x of equity held in that side; funding from Binance's settled rates (a proxy for Kraken's).

| | Universe | Long coins | Short coins | Long excess (t) | Short excess (t) | Long: funding paid / cost | Short: funding received / cost |
| --- | --- | --- | --- | --- | --- | --- | --- |
| In-sample 2022-05 to 2024-09 | +26 | +42 | +11 | +15.9 (0.9) | +14.9 (0.8) | 1.4 / 8.6 | 6.6 / 10.4 |
| Holdout 2024-10 to 2025-12 | +27 | +137 | -38 | +109.7 (3.8) | +64.7 (3.4) | 1.5 / 9.7 | 3.0 / 10.4 |
| 2022 (from May) | -102 | -54 | -140 | +48 (1.2) | +38 (1.0) | | |
| 2023 | +95 | +79 | +97 | -17 (-0.8) | -2 (-0.1) | | |
| 2024 | +97 | +168 | +46 | +71 (2.2) | +51 (1.8) | | |
| 2025 | -30 | +60 | -72 | +90 (3.0) | +43 (2.2) | | |

- **The short side is as real as the long side, and no more.** The shorted coins lagged the universe in three of four
  years, by about as much as the long coins led it, and the two sides failed together in 2023. There is no sign that
  the long side carries the basket alone.
- **Shorting did not cost funding: it was paid.** The shorted coins had positive funding on average, so the short
  side received 3-7% a year. The fear from the volatility study (shorts paying 25-60%) doesn't apply to this ranking.
- **In-sample neither side is distinguishable from zero** (t 0.8-0.9), and each side's trading costs (9-10% a year
  per 1x) are about two thirds of its in-sample excess. The strong holdout is 15 months.
- A long-only version would keep half the edge and all of the market exposure (the universe itself swung from -102%
  to +97% a year), which is the exposure the trend sleeves already carry.

**What this changes:** nothing in the structure. Keep it long/short if it is kept at all. The open question is
unchanged: the evidence is one strong holdout on a weak in-sample, and real Kraken spreads decide it (TODO section 2).
A model that switches the basket between long-only, short-only and both is not supported by this: both sides work
or fail together.

---

## 2026-10-03: A multi-strategy book under correlation-aware limits

The first book with strategies that are really different: five daily trend sleeves (BTC, ETH, SOL) plus the taker-buy
basket on 40 Kraken perps, `config/portfolio.multi_paper.toml`, run through the new risk model
(`src/portfolio/risk_model.py`: an EWMA covariance of instrument returns, 20-day half-life, correlations shrunk 20%
towards 0.5 and floored at 0.9 for the volatility cap). The limits were written into the config before the run and
not tuned: beta to BTC at most 1.0x, book volatility at most 40% at crash correlations, the altcoin group at most 1.0x
gross and 0.3x net. `python scripts/research/multi_book_study.py`; 2022-10-09 to 2025-12-31, holdout from 2024-10-01
(the frozen final holdout stays locked); 7 books logged (family `portfolio_construction`).

The six units (each trend sleeve, and the basket as one) correlate +0.38 on average in-sample and are worth **2.9
independent bets**: the five trend sleeves correlate 0.34-0.76 with each other and 0.01-0.05 with the basket.

| Allocation (limits on), IS / HO | Sharpe | CAGR | Vol | Max DD | Costs %/yr | Avg beta to BTC |
| --- | --- | --- | --- | --- | --- | --- |
| equal | 0.92 / 1.69 | 24% / 44% | 27% / 23% | 23% / 15% | 2.7 / 2.8 | 0.42 / 0.44 |
| risk_parity | 0.61 / 2.94 | 12% / 67% | 22% / 18% | 15% / 11% | 5.4 / 5.6 | 0.29 / 0.29 |
| hrp | -0.05 / 3.76 | -3% / 89% | 19% / 17% | 24% / 7% | 8.3 / 8.6 | 0.13 / 0.16 |

| Limits (allocation equal), IS / HO | Sharpe | Vol | Max DD | Max beta to BTC | Max vol at crash correlations |
| --- | --- | --- | --- | --- | --- |
| Plain caps only (gross, net, per instrument) | 0.95 / 1.67 | 32% / 24% | 26% / 16% | 1.20 / 1.17 | 63% / 53% |
| + group caps | 0.95 / 1.67 | 32% / 24% | 26% / 16% | 1.20 / 1.17 | 63% / 53% |
| + beta cap | 0.93 / 1.71 | 32% / 24% | 26% / 15% | 1.03 / 1.03 | 61% / 53% |
| + volatility cap | 0.92 / 1.66 | 27% / 23% | 23% / 16% | 1.01 / 1.08 | 43% / 43% |
| All (the config) | 0.92 / 1.69 | 27% / 23% | 23% / 15% | 1.00 / 1.01 | 43% / 43% |

- **Keep `equal`.** `risk_parity` and `hrp` (which can now treat a basket as one unit, by its own return) move
  capital from trend to the calm, uncorrelated basket. Chosen in-sample that loses (0.61 and -0.05 against 0.92): the
  basket made nothing in-sample on Kraken prices and costs 5-8% a year at these weights. Their holdout Sharpes (2.9,
  3.8) are the basket's lucky 15 months again (2026-09-29 entry), not evidence for the allocation.
- **The limits are insurance, at a small price.** In-sample the volatility cap cuts volatility from 32% to 27% and
  the worst drawdown from 26% to 23% for 0.03 of Sharpe; the book's largest volatility at crash correlations falls
  from 63% to 43%. The beta cap trims the few weeks the book amounted to 1.2x BTC. The caps hold on the targets;
  held positions sit a little over them (1.00-1.01 beta, 43% volatility) because prices drift between decisions and
  the rebalance band leaves small gaps alone.
- **The group caps never bound** in this book: under `equal` the basket is a sixth of the book, so altcoin gross
  peaked at 0.33x and net at 0.16x. They are there for a book where the basket is larger.
- The new limits default to off, so the BTC live book and the example book behave as before.

**What this changes:** the multi book is a paper candidate with `equal` allocation and all limits on. It doesn't
change the funding decision on the basket, which still waits for Kraken's real alt spreads (TODO section 2). More
uncorrelated sleeves, not a cleverer allocation, are what would raise the 2.9 bets.

Found on the way, and fixed the same day: the engine's `inverse_vol`, `risk_parity` and `hrp` scales differed from the
research backtest's after a cold start (by up to 3-6% of equity on the test books), because each counted its refit
schedule from its own first bar and the engine replayed its history with the sleeves' latest weights. Refits now follow
the calendar (a bar whose number since 1970 is divisible by the refit length), research allocates over the warmup
history too, and the engine steps sleeves and allocator together bar by bar; the engine parity test now covers every
method. The risk-parity and HRP rows above are from after the fix (before: 0.58 / 2.82 and -0.26 / 3.73). The
2026-09-28 table moves by a few hundredths too (example book: inverse_vol 1.46 / 1.00, risk_parity 1.44 / 1.16, hrp
1.39 / 1.37); its conclusion, keep `equal`, stands. `equal` and `fixed` were never affected.

---

## 2026-09-29: The taker-buy basket on Kraken's own prices, through the portfolio engine

The cross-sectional taker-buy book (2026-09-26 entry: top 30, 20% legs, every 10 days, chosen then) replayed with the
new basket sleeves (`src/portfolio/basket.py`) on Kraken perp candles for 40 Kraken-listed coins, Kraken taker fees
and slippage of 5-30 bps per side by Kraken 24h volume, the config's risk limits. The signal is still Binance's
taker-buy share. `python scripts/research/basket_backtest.py`; 1 trial logged. A coin joins only once Kraken lists its
perp, so the history starts 2022-05 (the Binance study started 2020-06).

| | Sharpe | CAGR | Max DD | Costs % of equity / yr |
| --- | --- | --- | --- | --- |
| In-sample 2022-05 to 2024-09 | 0.15 | 0.7% | 37% | 10.8 |
| Holdout 2024-10 to 2025-12 | 3.51 | 108% | 8.5% | 11.6 |
| 2x costs, in-sample | -0.63 | -14% | 41% (the 40% kill fired, flat after) | 16.7 |

- **The diversification holds:** daily correlation with the BTC trend book -0.02, beta to BTC ~0. A 30% blend in the
  holdout lifted the book's Sharpe from 1.04 to 3.31.
- **The edge doesn't:** in-sample on Kraken prices it made nothing. The Binance study's strong in-sample leaned on
  2020-21, before most of these Kraken perps existed. A 15-month holdout Sharpe of 3.5 is more likely luck than level.
- **Costs decide it.** Turnover is ~46x equity a year and the assumed alt slippage costs ~11% a year; at 2x it
  fails. Paper trading can't settle this (the paper sandbox charges the same assumed slippage). Kraken's real
  spreads and depth on these coins, from public order books, can.
- Found on the way: a governance gap. Series that start after 2025-01-01 kept only their own last 25% frozen, so a
  coin listed in 2025 could reach into the 2026 holdout. The first run of this study did (to 2026-06); it's fixed
  (`research_cutoff` never passes 2026-01-01 for a series that began before it) and rerun. The H1-H3 studies used their
  own 2026-01-01 cut and weren't affected.

**What this changes:** don't fund the basket yet. Measure Kraken's alt spreads and depth first; if real costs are
below the assumed tiers it becomes a candidate again, paper-run at a realistic equity. The basket machinery is
ready either way.

---

## 2026-09-28: Risk-parity and HRP allocation, and the books against BTC buy-and-hold

Two allocation methods ported (as ideas, rewritten) from the QIS repo: `risk_parity` (equal risk contribution) and
`hrp` (hierarchical risk parity), both on the covariance of the sleeves' own returns, with each sleeve's volatility
measured only while it holds a position. `python scripts/research/portfolio_backtest.py <config>`; in-sample to
2024-09-30, holdout 2024-10-01 to 2025-12-31 (the frozen final holdout stays locked). 4 configurations logged.

| Sharpe (IS / HO) | equal | inverse_vol | risk_parity | hrp |
| --- | --- | --- | --- | --- |
| Example book (5 sleeves, BTC + ETH) | 1.45 / 1.09 | 1.44 / 1.01 | 1.44 / 1.19 | 1.28 / 1.32 |
| Live BTC book (3 sleeves) | 1.50 / 1.02 | 1.50 / 1.02 | 1.54 / 1.03 | 1.57 / 1.16 |

- **Keep `equal`.** Chosen by in-sample numbers, neither method beats it on the example book, and on the live book the
  gain (0.04-0.07) is noise. The sleeves correlate 0.53-0.75, so there is little risk to rebalance between them.
  Revisit when the book has sleeves that are genuinely different (carry, cross-sectional, options).
- **Against BTC buy-and-hold** (new benchmark table, daily): the live book has beta 0.14 (IS) / 0.06 (HO), alpha 17% /
  8% a year and captures 13% / 7% of BTC's down days. The 4h MA sleeve's holdout alpha is negative (-6%), the one
  sleeve worth watching in paper trading.

---

## 2026-09-28: Three pre-registered hypotheses: crowding (H1), liquidation overshoot (H2), variance risk premium (H3)

Full reports in `research/reports/`, summary in `research/README.md`. Pre-registered in `research/prereg/` before any
result, run on BTC/ETH/SOL to 2025-12-31 with a new point-in-time harness (`src/research/pit.py`: every input carries
`available_at`; the holdout lock raises on explicit requests). Fills at the next open, stops on intrabar high/low,
actual funding settlements, volatility-scaled spreads, gross/net/2x-cost results, placebos, per-hypothesis trial
counts. The frozen holdout was **not** opened.

| | Result |
| --- | --- |
| H1a: OI-weighted 3-day funding, 90-day z, predicts lower 1-7d returns | **Fail, reversed sign.** Pooled slope +14 bps per z at 3 days (ETH, SOL positive; BTC ~0). Top quintile has the best 7-day mean and the thinnest left tail. The tilt overlay (Sharpe 0.4) beats only 75-79% of placebos |
| H1b: timed carry, enter when funding clears the 4-leg hurdle | **Passes development at Binance fees** (14-day hold: 9.8%/yr on notional, Sharpe 6.2, max DD 1.9%, placebo 100%), but 2025 earned 0.1% and the global deflated Sharpe is 0.83. At Kraken's 0.40% spot fee only the 30-day hold survives 2x costs |
| H2: liquidation-shaped hours (proxy: 3 sigma move, OI -3%, 5x volume) revert | **Fail.** 200 events; 12-48h CAR indistinguishable from vol-matched controls (placebo p 0.17-0.74); trades lose 27-77 bps net at stressed costs |
| H3-P1: DVOL > next-30-day realized vol | **BTC yes** (+5.9 pts, t 3.4, all 5 years), **ETH no** (+1.6, t 0.5) |
| H3-A1: size the live sleeves by IV instead of EWMA | **Fail.** +0.02 Sharpe, worse drawdown at matched vol (34.4% vs 32.9%) |

Also found and fixed: DVOL was aligned at its candle open, so hourly features could see up to an hour of future
implied vol (`positioning.load_positioning`); and raw `asi8` comparisons across ms/us/ns timestamps silently
dropped funding in the first H2/H3 runs (now always compared in ns).

**Holdout (one approved look, 2026-09-28):** carry at Binance fees, 14-day hold, Jan-Aug 2026: **-0.10%** pooled
(pass needed > 0). Funding averaged 2.5%/yr on BTC against a 9.9% hurdle, so the rule sat out seven months and lost
its one August round trip to costs. Always-on carry made +0.5%. H1b fails; carry is a regime trade for high-funding
markets only.

What this changes: don't build the funding-crowding filter on the trend rules (the sign is reversed at these
horizons); keep EWMA sizing; don't build a carry sleeve now. Liquidations and option chains are now being recorded for the real H2 and H3 phase-B tests.

---

## 2026-09-27: How many independent bets are the 13 strategies, and which features can pay for a trade?

First studies under the new governance (frozen final holdout from 2026-01-01; data to 2025-12-31).

**Strategy clusters** (`scripts/research/strategy_clusters.py`): every catalog strategy at its defaults,
long/short, 4h bars on the BTC and ETH perps, 2020-2025, daily returns before costs, clustered at correlation 0.7.

| Cluster | Members (mean correlation) | Sharpe of the best, before costs |
| --- | --- | --- |
| Trend | donchian_breakout, keltner_breakout, moving_average_crossover, trend_tstat (0.81) | keltner 0.80 |
| Volume momentum | volume_confirmed_momentum, _biased (0.81) | biased 1.06 |
| Singletons | volatility_squeeze (0.73 with trend), ema_vwap, momentum_breakout, signal_trend, trend_pullback | |
| Reversion | band_reversion, rsi_reversion | both negative |

- **The 13 strategies are worth 3.1 independent bets** (effective number from the correlation eigenvalues).
- The two reversion strategies are **the trend bet with the sign flipped** (correlation -0.6 to -0.87 with the
  trend cluster), which is why they lose whenever trend wins. They add no diversification, only the opposite bet.
- More price-only variants would add trials, not bets. New bets need new information: positioning, cross-sectional
  ranks, carry, order flow.

**IC decay by horizon** (`scripts/research/feature_decay.py`): the 21 features of the positioning study on 1h
bars, BTC and ETH, 2020-2025. Each feature at the horizon where its IC peaks, averaged over the two coins; the edge is
how far the best quintile's forward return sits from the average, against a ~20 bps taker round trip.

| Feature | Peak horizon | IC | t (non-overlapping) | Best quintile edge | Verdict |
| --- | --- | --- | --- | --- | --- |
| Past 4h return (vol-scaled) | 1h | -0.054 | -12.2 | 0.8 bps | real, too small for costs |
| Taker buy share, 1h / 4h | 1h | -0.036 / -0.033 | -8.0 / -7.6 | 0.8 bps | real, too small for costs |
| Past 1d return | 4h | -0.045 | -5.1 | 1.2 bps | real, too small for costs |
| Funding z-score (30d) | 1d | -0.047 | -2.1 | 29 bps | **the one candidate** |
| DVOL change 1d | 3d | +0.071 | 1.7 | 71 bps | noise alone |
| Retail / top-trader long-short | 7d | -0.085 / -0.073 | -1.2 / -0.9 | 150 / 107 bps | noise alone |
| 13 others | | | below 1.7 | | noise |

- **This explains every failed intraday study at once:** short-horizon price and flow effects are strongly
  significant (short-term reversal) but worth under 1 bp per trade, against a 20 bps round trip. No entry rule can
  turn that into money at taker costs.
- **Crowding is the only direction with room:** funding extremes predict the next day's return by more than the
  cost. The long-horizon crowding and implied-vol features have big bucket edges but only ~300 independent weeks, so
  each is noise alone; combined (the crowding composite, 2026-09-26 entry) they were more convincing.
- Next: build candidates from features, not rules: the funding z-score and a crowding composite as sleeves, tested
  against the trial ledger's count, and only then against the frozen holdout.

---

## 2026-09-27: EMA x VWAP (an Instagram day-trading rule) on BTC and ETH perpetuals, 15m and 1h

**Question:** the rule enters long when a candle closes up through the VWAP and exits when a candle closes back
below an EMA (posted for 3-minute candles, long only). Does it pay, and does a mirrored short side help? Strategy
`ema_vwap` (src/backtest/strategies.py): session VWAP reset at 00:00 UTC or rolling 96 bars, EMA 9/20/50, an
optional "EMA above VWAP" trend filter, optionally flat at the end of each UTC day. 24 combos x long-only and
long/short x taker and maker costs, Kraken perp candles 2020-02 to 2026-09, last 30% as holdout. Reproduce with
`python scripts/research/ema_vwap_study.py`.

| Costs | Interval | Side | Positive combos (IS) | Median Sharpe IS / HO | Best combo IS / HO | Buy and hold IS / HO |
| --- | --- | --- | --- | --- | --- | --- |
| Taker | 15m | long only | 0% | -3.41 / -4.67 | -1.93 / -1.30 | 0.99 / 0.49 |
| Taker | 15m | long/short | 0% | -5.02 / -6.13 | -2.76 / -2.42 | 0.99 / 0.49 |
| Taker | 1h | long only | 38% | -0.25 / -0.99 | 0.36 / -0.35 | 1.01 / 0.49 |
| Taker | 1h | long/short | 4% | -0.79 / -1.81 | 0.01 / -0.24 | 1.01 / 0.49 |
| Maker (optimistic) | 15m | long only | 17% | -0.55 / -0.63 | 0.34 / -0.60 | 0.99 / 0.49 |
| Maker (optimistic) | 1h | long only | 100% | 0.68 / 0.18 | 1.00 / 0.74 | 1.01 / 0.49 |
| Maker (optimistic) | 1h | long/short | 83% | 0.41 / -0.08 | 0.68 / 0.37 | 1.01 / 0.49 |

**Findings**
1. **Costs decide it, and the signal barely covers them.** The typical trade makes about 0.15% before costs at 1h
   and almost nothing at 15m; a taker round trip costs about 0.20%. At 15m every combination loses after taker
   costs (600-1,700 trades a year per coin), and at 1h the typical one does.
2. **Even with optimistic maker fills it doesn't beat holding.** The best case (1h, long only, maker, every limit
   order filled at the close) has a median holdout Sharpe of 0.18 against 0.49 for buy-and-hold, and most of its
   return is the long bias of a rising market.
3. **The short side makes it worse everywhere** (lower Sharpe in every cell). On BTC and ETH, closes through the VWAP
   downwards are followed less reliably than upwards ones, and shorting pays for every false break. The original long-only
   version is the better one; don't add the short leg.
4. **The in-sample ranking "carries over" (rank correlation 0.8-0.96 with taker costs) only because fewer trades
   lose less.** The best combos are the slowest ones (EMA 50, rolling VWAP, trend filter on), not the most skilful.
5. **3-minute candles, as posted, would trade 5x more often than 15m on smaller moves.** With 15m already losing
   ~0.02% a trade after optimistic maker costs, 3m is very unlikely to pay unless fills are both maker and better
   than the close. Not tested yet: it needs 1-minute candles (a download, `--intervals 3m --allow-download`).

Same conclusion as the earlier intraday study: short-horizon price rules on BTC and ETH don't clear Kraken's costs.

**Follow-up (same day): the posted version, 3-minute candles, on ten coins, perps and spot.** Kraken perp 1-minute
candles resampled to 3m (BTC, ETH, 2026-06-29 to 09-27), and Binance's archive of 3m candles for BTC, ETH, SOL, XRP,
DOGE, ADA, LINK, AVAX, LTC and DOT (June-August 2026, perps and spot), priced at Kraken's fees (perps 0.05% taker /
0.02% maker, spot 0.40% / 0.25%). Reproduce with `--intervals 3m --days 90 --allow-download`, and `--source binance
--market futures|spot --intervals 3m --allow-download`.

| Data (3m) | Side | Positive combos | Median Sharpe IS / HO, taker | Median, maker | Avg trade after taker costs | Buy and hold IS / HO |
| --- | --- | --- | --- | --- | --- | --- |
| Kraken perps, BTC+ETH | long only | 0 of 24 | -21.8 / -25.5 | -4.5 / -5.8 | -0.20% | 5.0 / 3.1 |
| Kraken perps, BTC+ETH | long/short | 0 of 24 | -28.7 / -35.4 | -7.2 / -11.0 | -0.20% | |
| Binance perps, 10 coins | long only | 0 of 24 | -18.5 / -18.7 | -5.8 / -4.2 | -0.21% | -1.5 / 4.2 |
| Binance perps, 10 coins | long/short | 0 of 24 | -24.0 / -26.1 | -6.2 / -7.0 | -0.21% | |
| Binance spot, 10 coins | long only | 0 of 24 | -43.6 / -43.8 | -32.8 / -32.7 | -1.01% | -1.5 / 4.2 |
| Binance spot, 10 coins | long/short | 0 of 24 | -58.1 / -58.5 | -42.7 / -44.0 | -1.01% | |

- **No edge before costs.** In every data set the average trade after costs equals minus the round-trip cost, give
  or take 0.01%: the 3-minute signal is worth about -0.01% a trade before fees, on every coin and both sides. No fee
  level can fix that; it is noise that trades 300-600 times per coin in three months.
- **Spot is far worse** only because Kraken's spot fee is eight times the perp fee.
- **Verdict: drop it.** The 3m version as posted, the slower 15m/1h versions and the short leg were all tested; none
  beats holding, and none has an edge before costs at 3m. Such posts usually show a few hand-picked winning trades,
  not a year of every trade.

---

## 2026-09-27: Does Kraken's minimum lot break the live BTC book at small capital?

**Question:** the research simulator trades continuous weights, but on Kraken Futures the book can only hold whole
0.0001 BTC lots (about 8.4 USD, 44% of a 19 USD account). Does the backtest still describe the live book?
`config/portfolio.btc_live.toml`, 2020-2026, full period, with the config's risk overlay and money cap, positions
rounded to whole lots after the overlay.

| Capital (USD) | Lots | Sharpe | CAGR | Max drawdown | Avg gross | Turnover/yr |
| --- | --- | --- | --- | --- | --- | --- |
| 19 | continuous | 1.41 | 22.7% | 16.8% | 0.22 | 8.7 |
| 19 | whole lots | 1.43 | 23.0% | 19.0% | 0.21 | 12.1 |
| 100 | whole lots | 1.00 | 6.4% | 9.5% | 0.10 | 5.4 |
| 5000 | whole lots | 0.89 | 0.3% | 0.4% | 0.003 | 0.1 |

**Findings**
1. **At 19 USD the lot size doesn't break the book**: the same Sharpe, 2 points more drawdown, and 38% more
   turnover (the book jumps between flat and one lot). The backtest is a fair guide to the live book.
2. **`max_gross_notional = 15` is a fixed dollar cap.** With more capital and the same config the book barely trades
   (gross 0.003 at 5,000 USD). Raise it with the capital: it was sized for one lot, on purpose.
3. **The expected money is small**: about 23% a year on 19 USD is roughly 4 USD a year. The first live run is a
   test of the machinery, not a source of profit.

**Follow-up (same day): the planner rounds toward zero, not to the nearest lot.** The table above rounded to the
nearest lot. The runtime's order planner rounds toward zero, so a target below one lot stays flat. Re-run with the
exact planner rule (`run_book(..., lots=True)`):

| Book at 19 USD | Sharpe | CAGR | Max drawdown | Avg gross | Turnover/yr |
| --- | --- | --- | --- | --- | --- |
| Continuous weights | 1.41 | 22.7% | 16.8% | 0.22 | 8.7 |
| Whole lots, rounded toward zero | 1.29 | 18.5% | 23.0% | 0.19 | 11.7 |
| Whole lots + small-account rule | 1.45 | 20.9% | 15.7% | 0.20 | 8.5 |

The small-account rule (`[portfolio] small_account_equity = 95`, about 1000 NOK): below it, a nonzero target
that rounds to zero lots holds one lot, if the lot is at most 90% of equity and fits the money cap. It is on in
`config/portfolio.btc_live.toml`. Above the threshold, normal rounding applies.

---

## 2026-09-26: Risk exits and account breakers for the single-strategy runtime

**Question:** the runtime's defaults (a 60-bar time stop, a 3x ATR stop from entry, a 5% position stop, a 5% daily
loss limit, and an inherited 2% hard stop) had never been tested. Do they help, and how often would the breakers
shut the runtime down? Every exit variant on each sleeve of `config/portfolio.btc_live.toml` (BTC perp: MA 4/48 1d
long-only, Keltner 40/2 1d long/short, MA 8/96 4h long-only), 2020-2026, holdout from 2024-10-01. Reproduce with
`python scripts/research/stop_study.py config/portfolio.btc_live.toml`.

| Variant | MA 1d Sharpe (IS / HO) | Keltner 1d | MA 4h | Book |
| --- | --- | --- | --- | --- |
| No stops | 1.58 / 0.87 | 1.08 / 0.82 | 1.55 / 0.76 | 1.50 / 1.28 |
| Old runtime defaults (5% + 60 bars + 3 ATR fixed) | 1.48 / 0.81 | 1.00 / 0.92 | 0.71 / 0.85 | 1.27 / 1.32 |
| 60-bar time stop alone | 1.20 / 0.83 | 0.86 / 0.90 | 0.63 / 0.83 | 1.09 / 1.30 |
| 3 ATR fixed from entry | 1.58 / 0.87 | 1.06 / 0.82 | 1.58 / 0.77 | 1.49 / 1.28 |
| 3 ATR trailing | 1.68 / 0.80 | 1.06 / 0.84 | 1.84 / 1.10 | 1.61 / 1.21 |
| 3 ATR trailing + 10% stop (new defaults) | 1.68 / 0.80 | 1.10 / 0.84 | 1.84 / 1.10 | 1.63 / 1.22 |
| 3 ATR trailing + 5% stop | 1.77 / 0.77 | 1.25 / 0.85 | 1.83 / 1.10 | 1.74 / 1.19 |

Account breakers, each sleeve sized like the runtime (a position worth 10% of equity) with the old exits:

| Sleeve | Max drawdown | Days losing > 5% | 2% hard stop: starts shut down within a year | 5% | 10% |
| --- | --- | --- | --- | --- | --- |
| MA 1d | 6.5% | 0 | 21% | 3% | 0% |
| Keltner 1d | 5.7% | 0 | 31% | 2% | 0% |
| MA 4h | 5.7% | 0 | 29% | 0% | 0% |

At full size (100% of equity) the max drawdowns are 46-50%, a 5% daily loss happens 20-49 times in six years, and
even a 20% hard stop shuts down 21-36% of starts within a year.

**Findings**
1. **The time stop was the damage.** It cut 0.4-0.9 of in-sample Sharpe from every trend sleeve (the 4h MA fell
   from 1.55 to 0.63), because trend profits come from the few trades held longest. The holdout moved a little the
   other way, but not enough to justify it. Removed from the defaults.
2. **A fixed ATR stop from entry almost never fires**; trailing it behind the best close does. On the 4h MA, the
   runtime's documented setup, it lifted Sharpe from 1.55 to 1.84 in-sample and 0.76 to 1.10 in the holdout,
   and cut the holdout drawdown from 23% to 17%. On the daily sleeves it is neutral within noise. Made the default.
3. **5% vs 10% position stop under the trailing stop is noise.** 5% wins in-sample and loses a little in the
   holdout. The default is 10%, as a catastrophe cap.
4. **The inherited 2% hard stop would have shut the runtime down routinely**: a normal strategy drawdown at 10% size
   (up to ~6.5%) crosses 2% below the start for 21-31% of start days within a year. The runtime now sets 10%
   explicitly, which never fired historically at 10% size, so a shutdown means something is wrong. A 5% daily loss
   never happened at 10% size; it stays as a disaster breaker.
5. **Both breakers assume the default 10% size.** At larger sizes they fire on normal volatility; raise them with
   the size (runbook). The portfolio runtime has its own limits in the TOML and is unaffected.

Caveat: 10 variants on three correlated BTC sleeves; only the time-stop result is large and consistent. Changed
in `build_runtime_orchestrator` (main.py).

---

## 2026-09-26: First fit to a live Deribit BTC option chain

**Question:** how well do the pricing models fit the market, in implied-vol points? One snapshot (992 BTC options,
13 expiries from 2 days to 1 year; 370 liquid out-of-the-money quotes). Reproduce with
`notebooks/options_research.ipynb`.

| Model | IV RMSE, all expiries | IV RMSE, 90-day expiry |
| --- | --- | --- |
| SVI surface (5 numbers per expiry) | 0.20 | 0.17 |
| Flat Black-76 per expiry (no smile) | 5.14 | 6.38 |
| Merton jumps (one parameter set) | 2.97 | 1.74 |
| Local vol + jumps PDE (fitted to the 90-day expiry only) | n/a | 1.95 |

**Findings**
1. **The term structure dominates.** At-the-money vol ran from 17.5% (2 days) to 40% (1 year). One Merton parameter
   set can't bend to it: its error is 12.7 vol points on the 2-day options and about 2 on the longer ones. A model
   meant for all expiries needs time-dependent parameters or stochastic volatility.
2. **The richer PDE model didn't beat Merton on one expiry** (1.95 vs 1.74 vol points, with its jump intensity near
   its upper bound). Extra parameters aren't worth it until they buy fit out of sample.
3. **Use the SVI surface for risk** (greeks and scenarios). It passes the validation gate once its wings beyond the
   quoted strikes are held at the edge vol. Fitted on a narrow range, a 20-day slice had extrapolated to a 150%
   vol and an impossible put price.
4. This is one snapshot. The model gate's out-of-sample and trading tests need the recorded history
   (`--record-option-chains`).

---

## 2026-09-26: Diversifying the trend book: more coins, and the taker-buy cross-sectional book

**Question:** the example book is 5 BTC/ETH trend sleeves that move together. What diversifies it: the same trend
rules on more coins, or the taker-buy share candidate from the cross-sectional study? Reproduce with
`python scripts/research/diversification_study.py`. Prices and funding come from Binance's daily perp archive, a
proxy for Kraken. The new multiple-testing tools are in `src/research/stats.py` (probabilistic and deflated
Sharpe).

**Short answer:** the taker-buy book is the diversifier. It has zero correlation with the trend book (-0.02); a 30%
blend lifted Sharpe from 0.61 to 0.80 in-sample and from 1.24 to 1.64 in the holdout, and cut the drawdown from 36%
to 26% and from 22% to 16%. More coins with the same trend rules only reduce drawdown: they are 0.71 correlated with
the core book and cost holdout return.

**Part 1: the same rules on 12 more Kraken-listed coins** (SOL, XRP, ZEC, DOGE, BNB, ADA, AVAX, LINK, NEAR, BCH,
LTC, FIL). MA 4/48 long-only and Keltner 40/2 long/short, vol-targeted, unchanged: no new parameters were fitted.
The period is 2021-05 to 2026-08, holdout from 2024-10.

| Book | Sharpe IS | Sharpe HO | CAGR HO | Max DD IS | Max DD HO |
| --- | --- | --- | --- | --- | --- |
| core (BTC/ETH, 4 daily sleeves) | 0.61 | 1.24 | 40% | 36% | 22% |
| alts (24 sleeves) | 0.61 | 0.73 | 17% | 34% | 30% |
| core 50% / alts 50% | 0.69 | 1.17 | 30% | 29% | 15% |

The alt sleeves alone are weak (median Sharpe 0.22 IS, 0.17 HO; 16 and 14 of 24 positive). They correlate 0.30
with each other and 0.33 with the core sleeves, but the alt book as a whole correlates 0.71 with the core book.
Trend in crypto is mostly one market factor. (The core's in-sample Sharpe here is lower than in the earlier study
because the alts' history moves the start to 2021-05, after the 2020-21 rally.)

**Part 2: taker-buy share, robustness** on the Kraken-listed universe: 45 configurations (universe 20/30/50 ×
legs 10/20/30% × rebalance every 3/5/7/10/14 days). In-sample is 2020-06 to 2023, holdout 2024 onward.
- 44/45 positive in-sample and 44/45 in the holdout; median Sharpe 1.40 IS and 1.19 HO. It is a broad plateau, not
  a spike. Rebalancing every 10-14 days is best: the signal is slow, and trading it faster only pays costs.
- Deflated Sharpe of the best in-sample cell (top 50, 30% legs, every 10 days: 1.90 IS, 1.65 HO): **0.94** after the
  77 configurations tried across both studies. That is just short of the usual 0.95 bar.
- Stress (holdout Sharpe):

| Configuration | Base | 2x costs | 3x costs | No funding | 3x costs, no funding |
| --- | --- | --- | --- | --- | --- |
| top 50, 20% legs, weekly (the first study's) | 0.97 | 0.58 | 0.19 | 0.61 | -0.17 |
| top 30, 20% legs, every 10 days | 1.88 | 1.65 | 1.42 | 1.56 | 1.10 |
| top 50, 30% legs, every 10 days | 1.65 | 1.33 | 1.00 | 1.16 | 0.51 |

  At 10-day rebalancing it survives triple costs and the loss of all funding income. The weekly version does not.

**Part 3: blends of daily returns** (core book plus a share in the other book, holdout from 2024-10). The taker book
here is the weekly configuration, deliberately the weaker one:

| Blend | Sharpe IS | Sharpe HO | Max DD IS | Max DD HO |
| --- | --- | --- | --- | --- |
| core alone | 0.61 | 1.24 | 36% | 22% |
| core + 30% alt trend | 0.65 | 1.18 | 31% | 18% |
| core + 30% taker-buy | 0.80 | 1.64 | 26% | 16% |
| core + 40% taker-buy | 0.86 | 1.77 | 24% | 14% |

**Caveats**
- "Kraken-listed" uses today's Kraken listings, a mild look-ahead in the universe. The full Binance universe gave
  similar numbers (0.96 / 0.90 weekly), so it isn't what drives the result.
- Slippage tiers come from Binance volume, and Kraken's altcoin books are thinner. That is why the 2-3x cost
  stress matters, and the 10-day versions pass it.
- Funding is Binance's, and Kraken's differs. The no-funding stress covers this.
- With small capital, a 30-50 coin basket means small orders per coin. Kraken's minimum sizes must be checked
  before paper trading it.

**Next:** build basket sleeves (portfolio plan 7.4) so the taker-buy book can run as a sleeve. The signal needs
Binance's public daily klines (taker-buy volume, no API keys); orders go to the Kraken perps. Paper-trade it next to
the trend book. Adding alt trend sleeves is optional: they cut drawdown but not return.

---

## 2026-09-26: Risk budget of the example portfolio (how big should the book be?)

**Question:** what does the example book (`config/portfolio.example.toml`, 5 trend sleeves on the BTC and ETH perps)
risk in money, and what size fits a given drawdown limit? Reproduce with
`python scripts/research/risk_budget.py config/portfolio.example.toml --capital 10000 --max-drawdown 0.2` (module
`src/portfolio/risk_budget.py`; the size knob is `[portfolio] scale`, which research and the runtime both apply).
The period is 2020-09 to 2026-09, with perp costs, funding at 0.01%/day, and the config's risk limits.

| Scale | CAGR | Vol | Sharpe | Max DD | Longest under water | Worst day | Worst week | Worst month | Max gross |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.25 | 13.9% | 9.6% | 1.41 | 9.0% | 422 days | 3.8% | 4.5% | 3.6% | 0.42x |
| 0.38 | 21.6% | 14.6% | 1.41 | 13.3% | 421 days | 5.3% | 6.3% | 5.7% | 0.63x |
| 0.50 | 28.2% | 19.0% | 1.40 | 17.4% | 422 days | 6.8% | 8.2% | 7.5% | 0.81x |
| 1.00 | 53.4% | 34.7% | 1.41 | 31.3% | 508 days | 13.3% | 15.7% | 14.1% | 1.12x |

**Findings**
1. **Size changes risk, not quality.** Sharpe is about 1.4 at every scale; the drawdown and the worst days scale
   almost linearly. The choice is only how much loss you will sit through.
2. **The hard part is time, not depth.** At every scale the book spent over a year below a previous peak, and half of
   all months lost money. That is what makes people switch a system off at the worst moment, so plan for it.
3. **For a 20% drawdown limit with a 1.5x safety margin** (future drawdowns are usually deeper than past ones), the
   scale is 0.38: about 22% a year historically, with a 13% max drawdown and a 5% worst day. Full size (1.0) means
   living with a 31% drawdown and a 13% day.
4. **Margin is not the constraint.** Even at full size the book needs at most 80% of equity as initial margin at 2x,
   and 31% at scale 0.38.

**Before live:** pick the capital and the drawdown you can live with, set `scale` and `initial_equity`, and start
smaller than the table suggests until the paper and dry-run soak agree with the backtest.

---

## 2026-09-26: Cross-sectional features across Binance USDT perpetuals, 2020-2026 (current and delisted coins)

**Question:** do any of the 8 ranking features in `scripts/research/cross_sectional_study.py` make a market-neutral
long/short book (long the top 20% of the 50 most traded coins, short the bottom 20%) that survives costs and funding?
And does that hold on the coins Kraken Futures lists? **Short answer:** one candidate, the taker-buy share. It is
positive in both periods on both universes, with a low drawdown and no BTC correlation, but it is modest and about
half of it is funding received. The strongest effect in the data, "high-volatility coins underperform", isn't
tradable as a plain long/short: shorting those coins pays 25-60% a year in funding. Reproduce with
`python scripts/research/cross_sectional_study.py` (add `--kraken-only`). The data is Binance's public archive
(daily klines and funding for 859 symbols, delisted ones included, so there is no survivorship bias), cached in
`data/historical_cache/binance_um/`. In-sample 2020-06 to 2023, holdout 2024 to 2026-08. Costs are 0.05% taker
plus slippage by liquidity tier (3-25 bps). Signs were fixed in-sample.

**Information coefficient** (rank correlation with the next 7 days' return, holdout, t-stat on non-overlapping weeks):

| Feature | Full universe | Kraken-listed | Years with the same sign |
| --- | --- | --- | --- |
| vol_30d (30-day volatility) | -0.21 (t -9.8) | -0.19 (t -7.5) | 7/7 |
| max_ret_30d (best day in 30) | -0.18 (t -9.7) | -0.16 (t -7.4) | 7/7 |
| mom_30d | -0.04 (t -1.9) | -0.05 (t -2.0) | 6/7 |
| taker_buy_share_7d | 0.00 (t 0.3) | 0.03 (t 1.7) | 6/7 |
| funding_7d, mom_7d, ret_1d, volume_trend | about 0 | about 0 | 2-5/7 |

**Long/short books, weekly rebalance** (Sharpe IS / HO, max drawdown HO):

| Feature | Full universe | Kraken-listed | Where the holdout money comes from (Kraken-listed, % of equity a year) |
| --- | --- | --- | --- |
| taker_buy_share_7d (+) | 0.96 / 0.90, DD 29% | 1.11 / 0.97, DD 22% | price legs +26, funding +9.5, costs -10 |
| vol_30d (-) | -0.54 / -0.40 | -0.56 / 1.18 | price legs +71, funding -23, costs -5 |
| funding_7d (-, i.e. carry) | 1.24 / -0.70 | 1.19 / 0.42 | funding +28, price legs -5 |
| composite of the IS-significant features | -0.29 / -0.88 | 0.08 / 1.40 | |
| short-term reversal (mom_7d, ret_1d; daily) | -2.8 to -3.2 IS | similar | turnover 230-550x a year: costs 37-88% a year |

**Findings**
1. **The volatility (lottery) effect is real, and it is priced through funding.** High-volatility coins underperform
   every year (IC -0.09 in-sample, -0.2 holdout). But they are the coins with deeply negative funding, so the short
   leg pays 25-60% a year and the book loses in-sample on both universes. The Kraken holdout (1.18) disagrees with
   its in-sample (-0.56), which is not something to trade.
2. **Taker-buy share over 7 days** (coins whose volume is taker buying, going long) is the only feature positive in
   both periods on both universes, with 22-29% holdout drawdowns and a BTC correlation of about -0.05. Its IC is
   small (t 1.7-2.9), and a good part of the return is funding received (the long leg tends to hold coins where
   shorts pay). Eight features were tested on two universes, so a t of 2-3 is weak evidence on its own.
3. **Carry (short high-funding coins, long low-funding ones) decayed:** 1.2 Sharpe in-sample, -0.7 to 0.4 in the
   holdout, the same decay the BTC/ETH carry study found.
4. **Short-term reversal exists in the IC** (1-day IC -0.03 to -0.04, t -5), but daily rebalancing turns the book
   over 230-550 times a year, and costs are far larger than the edge.

**Next:** taker-buy share is a candidate for a cross-sectional sleeve (portfolio plan 7.4), not for trading yet. The
steps: a finer test (the rebalance interval, and the universe size around 30-50), its correlation with the trend
book, and a multiple-testing adjustment (the TODO's deflated-Sharpe item). Live, it needs Binance's public daily
klines for the taker-buy volume (no API keys) and would trade the Kraken-listed perps. The data for the altcoin carry study
(`premium_1d`, the perp's basis to spot) is downloaded too.

---

## 2026-09-26: Multi-sleeve portfolio of trend rules on the BTC and ETH perpetuals

**Question:** does a book of the trend rules that held up (MA crossover and Keltner, daily and 4h, BTC and ETH) beat
its sleeves? And which allocation and risk limits should the portfolio default to? **Short answer:** yes, mostly
through a smoother holdout. Default to `equal` allocation, and to a 40% drawdown kill without de-risking.
Reproduce with `python scripts/research/portfolio_backtest.py config/portfolio.example.toml` (module
`src/portfolio/backtest.py`: the same sleeve, allocation, netting and risk functions the runtime will use).

**Setup:** the 5 sleeves in `config/portfolio.example.toml`, each sized at entry to 50% annual vol (EWMA), on a 4h
grid (daily decisions land on the day's last 4h bar). Kraken perp taker fees (0.05%) plus 5 bps slippage, funding
0.01%/day on longs, 2% rebalance band. In-sample 2020-09-13 to 2024-09-30, holdout from 2024-10-01 (the split the
perp studies use). The sleeve parameters come from the full-history study with that same split, so treat the
holdout as a check, not proof.

| | Sharpe IS | Sharpe HO | CAGR IS | CAGR HO | Max DD IS | Max DD HO |
| --- | --- | --- | --- | --- | --- | --- |
| btc_ma_1d alone | 1.57 | 0.88 | 107% | 28% | 61% | 33% |
| eth_ma_1d alone | 1.23 | 0.94 | 58% | 30% | 42% | 35% |
| btc_keltner_ls alone | 1.07 | 0.81 | 55% | 31% | 52% | 46% |
| eth_keltner_ls alone | 0.79 | 1.18 | 31% | 56% | 64% | 28% |
| btc_ma_4h alone | 1.54 | 0.76 | 72% | 23% | 41% | 39% |
| **equal book, no risk limits** | 1.53 | 1.28 | 71% | 39% | 34% | 26% |
| equal book, first example limits (de-risk 15-30%, kill 30%) | 1.40 | 1.13 | 55% | 30% | 29% | 23% |
| **equal book, new example limits (kill 40%, no de-risk)** | 1.45 | 1.35 | 60% | 40% | 33% | 22% |
| inverse_vol book, new limits | 1.44 | 1.29 | 60% | 37% | 33% | 21% |

**Findings**
1. **The book diversifies.** In-sample its Sharpe matches the best sleeve, with about half of the worst sleeve
   drawdowns. In the holdout it beats every sleeve (1.28-1.35 vs 0.76-1.18). Daily return correlations between
   sleeves are 0.26-0.74: the three BTC sleeves correlate 0.49-0.74, and BTC with ETH 0.38-0.64. So five sleeves
   are far from five independent bets.
2. **Allocation barely matters here.** `fixed` equals `equal` because the budgets are equal. `inverse_vol` is
   slightly worse, because the sleeves are already sized to a volatility target, so scaling by instrument
   volatility adds nothing. Default: `equal`. Revisit `inverse_vol` for `fixed_fraction` sleeves.
3. **The `max_drawdown` halt is a kill, not a pause.** Once flat, the book's drawdown can't recover, so it stays
   flat. With a 30% kill and no de-risking it tripped and the book sat flat through the whole holdout (Sharpe 0).
   The first example only survived because its de-risking held the drawdown at 29%. Set the kill above the book's
   worst drawdown (33% here), and give the runtime an explicit reset.
4. **De-risking on drawdown hurts trend sleeves.** Scaling down from 15% drawdown cut in-sample Sharpe from 1.46 to
   1.40 (caps alone vs caps plus de-risking). Trend books recover by trending again, and de-risking leaves them
   under-sized when that happens. From 25% to 45% it made no difference. It is now off by default
   (`drawdown_derisk_start` unset).
5. **The other limits are roughly neutral.** Caps (gross 1.5, net 1.0, per instrument 1.0) move in-sample
   Sharpe from 1.53 to 1.46 and holdout from 1.28 to 1.35. The 6% daily-loss limit changes almost nothing
   (1.53 → 1.52). Keep both as safety rails.
6. **Funding at 3x the assumed rate** (0.03%/day) costs about 0.06-0.08 Sharpe. The book is mostly long.

**Next:** the order planner and the research/runtime parity test (portfolio plan 2.6-2.7). Add sleeves that are
less correlated with BTC trend (the cross-sectional study once the Binance archive is downloaded, and carry).

---

## 2026-09-26: Funding carry on BTC and ETH, per venue (long spot, short perp)

**Question:** what does the classic carry trade earn after fees? It holds long spot and short perp, so the price
risk cancels and the position collects funding when longs pay. **Short answer:** 11-14% a year on notional over
2019-2026 on Binance and Bybit, but almost all of it came in bull markets. In the last 12 months it netted about
1-3% a year, less than cash. Not worth building for BTC and ETH now. Reproduce with `python scripts/research/carry_study.py`
(module `src/research/carry.py`; cached funding plus one small Kraken API call).

**Funding level** (mean, % a year that a short receives):

| | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 | All | Negative days |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BTC Binance | 17.2 | 30.6 | 4.2 | 7.9 | 11.9 | 5.1 | 2.9 | 11.6 | 12% |
| ETH Binance | 27.4 | 37.5 | 0.8 | 8.3 | 12.9 | 4.9 | 1.9 | 13.8 | 12% |
| BTC Deribit | 9.1 | 16.4 | -2.3 | 6.8 | 10.2 | 5.4 | 2.0 | 7.5 | 27% |
| BTC / ETH Kraken (last 12 months only) | | | | | | | | 3.2 / 3.1 | 26% |

Bybit averages close to Binance (BTC 12.8, ETH 14.0 a year) but ran higher in 2021 (BTC 38.3, ETH 43.0).

**Net carry after fees.** Round trips (both legs, in and out, taker plus 2 bps slippage per leg) are 0.38% on
Binance, Bybit and Deribit, and 0.98% on Kraken, whose spot fee is 0.40%. Returns are % of notional a year, and on
capital at 2x perp leverage (1.5 units of capital per unit of notional):

| | Always on, full history | Filter (in above 10%/yr, out below 0) | Always on, last 12 months |
| --- | --- | --- | --- |
| BTC Binance | 11.5 (7.7 on capital), worst 30 days -1.3% | 9.5, ~2 entries a year | 3.0 (2.0) |
| ETH Binance | 13.7 (9.2), worst 30 days -1.8% | 12.4 | 2.1 (1.4) |
| BTC Bybit | 12.7 (8.5) | 11.0 | 2.4 (1.6) |
| BTC Deribit | 7.4 (5.0), worst 30 days -3.5% | 6.4 | 2.3 (1.5) |
| BTC Kraken | | | 2.2 (1.4) |

- **It's a bull-market trade.** In 2020-21 Binance and Bybit paid 17-38% a year on BTC and 27-43% on ETH, then about
  zero in 2022 and 2-5% in 2025-26. The full-history
  average is mostly those bull years.
- **Switching off in bad times doesn't help.** Funding is persistent, so the filter pays twice a year in switching
  costs and misses funding while it waits to re-enter. It was below always-on everywhere except ETH on Deribit. In
  the last 12 months funding rarely reached 10%, so the filter barely traded.
- **The venue matters.** Deribit's inverse perps pay 35-60% less funding, with two to three times as many negative
  days.
  Kraken's funding has recently matched Binance's, but its spot fee triples the round trip.
- **The downside is small but not zero.** The worst 30 days held continuously lost 1.3-4.5% of notional, when
  funding turned negative in sell-offs.
- **Not modelled:** the basis at entry and exit, moving collateral between the legs after big moves (the short
  perp needs margin top-ups in rallies), and exchange risk (FTX).

**What this points to.** For BTC and ETH, keep carry as a regime trade: switch it on by hand when funding runs above
~15-20% a year, as in 2021 or early 2024. The open question is altcoins, where funding is higher and more
dispersed. That needs the Binance archive (on wifi) and its premium index for the basis. Funding is already one of
the features in the cross-sectional study.

---

## 2026-09-26: Volatility forecasts and volatility-scaled sizing, BTC and ETH perpetuals 2021-2026

**Question:** which volatility forecast is most accurate, and does sizing positions by it improve the strategies that
held up on the full history? Reproduce with `python scripts/research/volatility_study.py` (module:
`src/research/volatility.py`). Everything is measured from 2021-03-01, when HAR's first walk-forward fit is
available.

**Forecast accuracy** (daily forecasts scored against realized variance from 1h bars; QLIKE is lower-is-better; mean
ratio is realized / forecast, where 1 is unbiased):

| Forecast | QLIKE next day (BTC / ETH) | QLIKE next 7 days | log R², 7 days | Mean ratio, 7 days |
| --- | --- | --- | --- | --- |
| Rolling std of the last 10 daily returns (what the runtime uses) | 0.69 / 0.69 | 0.57 / 0.64 | 0.35 / 0.32 | 1.77 / 1.80 |
| Rolling std, 30 days | 0.53 / 0.54 | 0.31 / 0.35 | 0.33 / 0.29 | 1.27 / 1.26 |
| EWMA of daily returns, 10-day half-life | 0.46 / 0.45 | 0.25 / 0.27 | 0.40 / 0.38 | 1.16 / 1.13 |
| EWMA of daily realized variance, 5-day half-life | 0.45 / 0.44 | 0.25 / 0.26 | 0.43 / 0.43 | 1.17 / 1.18 |
| HAR-RV, walk-forward | **0.41 / 0.40** | **0.20 / 0.21** | **0.46 / 0.46** | 0.92 / 0.96 |

- HAR is the most accurate at both horizons and the only unbiased one, as the literature says. EWMA is close and
  needs no fitting.
- **The runtime's 10-bar window is the worst**, and it runs low. The next week's realized variance was on average
  1.8x its forecast, because ten days of returns usually miss the spikes.

**Sizing.** Each strategy's long/flat/short targets are scaled to 50% annualised volatility (the size is 0.5 / the
forecast, capped at 2x equity), with taker fills and perp costs. "Entry-only" sizes a position when it opens and
never resizes it, as the runtime does. "Rebalanced" resizes when the ideal size moves more than 25%. A lower drawdown
alone proves nothing when the average size changes, so the fair checks are:
- Sharpe;
- the drawdown of the full-size strategy levered to the same volatility;
- a placebo: 200 runs with the forecast shuffled in time (same sizes, no timing), where "beaten" is the share of
  placebos with a lower Sharpe.

| Sharpe (placebos beaten) | Full size | Runtime 10-bar, entry-only | EWMA, entry-only | HAR, entry-only |
| --- | --- | --- | --- | --- |
| BTC `moving_average_crossover(4, 48)` 1d long-only | 0.65 | 0.85 | 0.85 (94%) | 0.86 (95%) |
| BTC `moving_average_crossover(8, 96)` 4h long-only | 0.54 | 0.65 | 0.85 (100%) | 0.90 (100%) |
| BTC `keltner_breakout(40, 2)` 1d long/short | 0.56 | 0.68 | 0.71 (88%) | 0.73 (90%) |
| BTC `keltner_breakout(40, 2)` 1d long-only | 0.71 | 0.83 | 0.80 (76%) | 0.74 (66%) |
| BTC `donchian_breakout(40, 10)` 1d long-only | 0.26 | 0.30 | 0.22 (40%) | 0.10 (6%) |
| ETH `moving_average_crossover(4, 48)` 1d long-only | 0.71 | 0.85 | 0.78 (86%) | 0.85 (98%) |
| ETH `moving_average_crossover(8, 96)` 4h long-only | 0.54 | 0.58 | 0.66 (90%) | 0.81 (98%) |
| ETH `keltner_breakout(40, 2)` 1d long/short | 0.63 | 0.70 | 0.70 (86%) | 0.76 (92%) |
| ETH `keltner_breakout(40, 2)` 1d long-only | 0.61 | 0.60 | 0.58 (54%) | 0.59 (60%) |
| ETH `donchian_breakout(40, 10)` 1d long-only | 0.32 | 0.30 | 0.28 (40%) | 0.24 (36%) |

- **Sizing at entry helps MA crossover and long/short Keltner beyond chance.** On both coins, these beat 86-100% of
  placebos. Their drawdown also falls against full size at the same volatility. BTC MA crossover goes from 64% to
  57% (EWMA) or 55% (HAR), and BTC 4h MA crossover from 51% to 42%.
- **It doesn't help Keltner long-only or Donchian.** Those are within placebo noise or worse. Breakout rules enter
  when volatility has just jumped, so vol-scaling shrinks exactly the trades they live on.
- **The forecast matters on 4h bars, not on daily.** On daily bars, the runtime's 10-bar window sizes entries about
  as well as EWMA or HAR. On 4h bars, 10 bars is only 40 hours, and HAR or EWMA add 0.1-0.25 Sharpe over it.
- **Resizing open positions mostly hurts.** Rebalanced was worse than entry-only in 9 of 10 cases with HAR and 6 of
  10 with EWMA, and tripled the orders. Crypto volatility rises during strong rallies too, so resizing cuts the
  trend trades that pay.
- **Don't vol-time buy-and-hold.** Rebalanced buy-and-hold was 0.42 vs 0.41 (BTC, EWMA) and 0.30 with HAR. The
  noisiest forecast happened to do best on ETH, which says it's noise.

**Caveats.** One 5.5-year period. The ten strategy-coin pairs are correlated (two coins, overlapping rules). The
strategies and parameters were chosen earlier on this same history, and the target and band were fixed before
looking. Only the ranking between sizings is tested here, not the strategies themselves.

**What this changes.** Size entries by an EWMA (or HAR) volatility forecast with an annualised target, don't resize
open positions, and prefer MA crossover and long/short Keltner as the rules to run with it. The runtime gains an
opt-in `--target-annual-vol` for this (see `runbook.md`).

---

## 2026-09-26: Positioning data (funding, open interest, trader ratios, implied vol), BTC and ETH

**Question:** does derivatives positioning predict BTC/ETH returns over hours to days, net of costs? **Short answer:
crowding does, modestly and contrarian, at 1-3 days. It is not a standalone strategy yet, but it is the first
information in this project that survives taker costs at these horizons.** Reproduce with
`python scripts/research/positioning_study.py`; data and alignment are described in `research_guide.md`.

**Data:** hourly Kraken perp bars (2020-2026) with funding (Binance, Bybit, Deribit), open interest (Binance, Bybit),
Binance top-trader and all-account long/short ratios, taker buy share, and Deribit DVOL. Each bar only sees values
published by its close. The Binance ratios start in late 2021 and have months-long gaps in 2022. No exchange publishes
liquidation history, so liquidations were not tested.

**Single features** (rank correlation with the forward return, years with the same sign in brackets):

| Feature | BTC 24h | BTC 72h | ETH 24h | ETH 72h |
| --- | --- | --- | --- | --- |
| Funding vs its 30-day average (z) | -0.034 (6/7) | -0.030 (5/7) | -0.054 (7/7) | -0.064 (6/7) |
| Funding change over 1 day | -0.035 (5/7) | -0.023 (6/7) | -0.031 (7/7) | -0.027 (6/7) |
| Binance open-interest change over 1 day | -0.031 (6/6) | -0.011 (3/6) | -0.037 (5/6) | -0.024 (5/6) |
| All-account long/short ratio | -0.030 (4/6) | -0.064 (5/6) | -0.015 (3/6) | -0.039 (4/6) |
| Top-trader long/short ratio | -0.023 (5/6) | -0.048 (6/6) | -0.014 (5/6) | -0.032 (5/6) |
| DVOL change over 1 day | +0.041 (4/6) | +0.081 (6/6) | +0.032 (4/6) | +0.050 (5/6) |
| Volume vs its 30-day average (z) | +0.019 (6/7) | +0.024 (5/7) | +0.025 (5/7) | +0.024 (6/7) |
| Last 4h return (vol-scaled), for comparison | -0.018 (6/7) | +0.005 (3/7) | -0.015 (5/7) | +0.006 (3/7) |

- **Crowding is contrarian and consistent across both coins.** Rising funding, rising open interest and a crowd
  leaning long all come before lower returns over the next 1-3 days. Unlike the price features, these ICs grow from
  4h to 24-72h, which is the horizon where a 20 bps round trip matters less.
- **Rising implied vol comes before higher returns** (DVOL change, the strongest single feature at 72h). That fits
  fear being paid for: implied vol jumps into sell-offs that then rebound. It has only 6 years of history.
- The raw funding level and Deribit funding switch sign between BTC and ETH. Bybit open interest and the "OI up with
  price up" interaction add little. The taker buy share also points in opposite directions on the two coins at 1-3
  days, so the composite below doesn't use it.

**Walk-forward ridge over all 21 features** (24h horizon, refit monthly, out-of-sample from 2022-12-14): IC +0.022
(BTC) and +0.004 (ETH), negative in 2026 on both. The deciles aren't monotone. After taker costs, BTC long-only is
positive from a 25 bps threshold (Sharpe 0.5-0.8), but ETH only at 100 bps (1.2 long-only, 0.9 long/short, about 0.5
trades a week). That is one of 8 threshold/side cells on ETH, so treat it as luck until it holds up elsewhere. The
first fits trained on 2022 alone, where the ratio gaps drop most rows.

**Unfitted crowding composite:** minus the average 30-day z-score of funding, the 1-day OI change, and the all-account
and top-trader long/short ratios. It has no fitted weights and its signs come from the crowding hypothesis. It was
traded over the same out-of-sample period, entering above a threshold and exiting when the sign flips:

| | IC 24h | IC 72h | 72h IC by year (2023/24/25/26) | Top decile, next 24h | Bottom decile |
| --- | --- | --- | --- | --- | --- |
| BTC | +0.029 | +0.047 | 0.12 / 0.00 / 0.07 / 0.01 | +25 bps (t 2.6) | -27 bps (t -1.5) |
| ETH | +0.047 | +0.071 | 0.16 / 0.13 / -0.02 / 0.02 | +46 bps (t 2.5) | -18 bps (t -1.6) |

| Sharpe, taker at close (maker, requote) | z 0.5 | z 1.0 | z 1.5 | Buy and hold |
| --- | --- | --- | --- | --- |
| BTC long/short | 0.32 (0.62) | 0.33 (0.44) | 0.31 (0.37) | |
| BTC long-only | 1.01 (1.20) | 0.90 (0.99) | 0.97 (1.04) | 1.12, max drawdown 54% |
| ETH long/short | 0.32 (0.51) | 0.39 (0.47) | 0.64 (0.68) | |
| ETH long-only | 0.52 (0.65) | 0.59 (0.64) | 0.89 (0.92) | 0.61, max drawdown 69% |

- **It beats the fitted model.** With four features and no weights, it has a higher out-of-sample IC than the ridge
  on both coins and makes money after taker costs in all 12 cases. The ridge was mostly fitting noise.
- **The long/short version is the clean test, and it is weak.** Sharpe 0.3-0.6 after taker costs, 3-16% a year,
  with 35-85% drawdowns. Most of the edge sits in the extreme deciles and in one or two years per coin (2023 on
  both).
- **The long-only version is mainly beta with lower exposure.** On BTC it roughly matches buy-and-hold's Sharpe while
  in the market 7-33% of the time, with a 10-34% drawdown instead of 54%. On ETH it beats holding (Sharpe 0.9 vs 0.6
  at z 1.5), but that rests on about 20 trades.
- **Caveats.** The four inputs and the sign were chosen from the crowding hypothesis, but after the full-sample
  feature ICs had been seen, and those include the test period. Three thresholds and two coins were tested.

**What this points to next.** Use crowding as an input rather than as its own strategy:
1. As a filter on the daily trend rules, e.g. skip or delay entries while longs are crowded.
2. As a ranking signal in the cross-sectional strategy (roadmap item 4), where funding and OI differences between
   coins are larger than one coin's differences over time.
3. As the timing input for funding carry (item 5).

Record liquidations live (item 3) if cascades are to be tested.

---

## 2026-09-26: Realistic limit-order fills remove the intraday maker edge

`src/research/execution.py` now simulates resting post-only limit orders: an order at the signal close fills only
if a later bar trades through it by `through_bps` (1 bp by default), all-or-nothing at the limit price; unfilled
orders are cancelled, re-quoted at the new close, or crossed as taker orders. Use `fills=FillModel.maker(...)` in
the API or `--fills maker` on the research CLI; `scripts/research/intraday_study.py` compares all variants.

**15m walk-forward forecast, 2021-2026, Sharpe** (thresholds in bps of forecast 4h return):

| | zero cost | taker at close | maker fee, always filled (old shortcut) | maker, cancel after 1 bar | maker, requote | maker, then taker after 2 bars |
| --- | --- | --- | --- | --- | --- | --- |
| ETH 30, long/short | 1.31 | 0.07 | 1.07 | 0.14 | 0.35 | 0.33 |
| ETH 20, long/short | 1.33 | -0.89 | 0.89 | -0.43 | -0.29 | -0.43 |
| BTC 20, long/short | 0.79 | -0.98 | 0.44 | -0.91 | -0.40 | -0.49 |

- **The old maker shortcut was a mirage.** Every realistic variant is far below it, and BTC is negative in all of
  them.
- **It isn't about missed fills alone.** 88-89% of orders fill even when giving up after one bar, and nearly all
  with re-quoting. The damage is adverse selection: the orders that fill are the ones where price moved against the
  new position first, and the trades that ran away were the good ones.
- **Slow strategies don't care.** Daily long-only Keltner and MA crossover (2020-2026) have Sharpe within +/-0.05
  of each other under every execution variant, because they trade a few times a year.

**Consequence:** with OHLCV features, an intraday edge has to survive taker costs on its own. The next steps are
better information (positioning, funding, order flow), not cheaper execution assumptions.

---

## 2026-09-26: Intraday, 15m and 1h bars, BTC and ETH perpetuals 2020-2026

**Question:** is there an edge at holding periods of hours, net of costs? **Short answer: not from price and volume
features alone at our fee tier.** Reproduce with `python scripts/research/intraday_study.py --interval 15m` (or
`1h`).

**Data check first.** The inverse `PI_` contracts went quiet after 2022, which made 42-69% of recent 15m bars stale
and would have manufactured fake reversal signals. History is now stitched from `PI_` (to 2022) and the linear `PF_`
contracts (from 2023): at most 0.7% flat 15m bars in any year.

**Single features** (rank correlation with the next 4h return, `src/research/features.py`):
- **Short-term reversal is real and consistent.** The last 4h move, the last day's move, the distance from the daily
  mean and the position in the daily range all predict the next 4h with IC -0.03 to -0.046, the same sign in 6-7 of
  7 years on both coins. It is still there on Kraken's index-price candles (IC -0.056 vs -0.066 on trades for the
  1h version), so it is not just bid-ask bounce. But it lives in the many small moves (the plain return
  autocorrelation is about 0), so it is worth only a few bps.
- **Late US session.** The 20:00-22:00 UTC hours were up +4.5 / +3.1 bps (BTC) and +5.3 / +3.9 (ETH) on average
  since 2020, 5-6 of 7 years positive, followed by a negative 22:00 hour. Since mid-2022 only the 21:00 hour holds up
  (+3.4 / +4.2 bps on index prices, t about 2.5). With 48 hour-coin combinations tested, treat it as a lead, not a
  finding.
- The other coin's last move, volume shocks and the volatility ratio add little.

**Combined forecast** (`src/research/forecast.py`: walk-forward ridge over 14 features, refit monthly on past data
with a purge, out-of-sample from February 2021; trade when the forecast exceeds a threshold, hold until it changes
sign):
- **It works before costs.** 15m out-of-sample IC +0.018 (BTC) / +0.022 (ETH), a monotone decile table, and Sharpe
  1.2 / 1.7 at zero cost.
- **The edge per trade is too small.** The strongest forecasts are worth about 5-13 bps over 4h, against ~20 bps for
  a taker round trip. At taker fees every threshold loses money.
- **Maker fees make it marginal.** ETH long/short reaches Sharpe about 1.0 when it only trades forecasts above
  20-30 bps; BTC reaches 0.3-0.4. That assumes every limit order fills at the bar close, which is optimistic: real
  limit orders fill mostly when price moves against them.
- **It is fading.** The out-of-sample IC turned slightly negative in 2025 and 2026 on both coins.
- 1h bars tell the same story with a weaker signal (IC +0.008 / +0.014).

**What this means.** A few basis points per trade is the typical size of edges in liquid crypto majors at these
horizons. With these costs and these features, there is nothing to trade yet. Getting an intraday edge needs either
better information (order flow, positioning, funding, cross-market data), cheaper execution (maker fills that are
modelled realistically), or a different kind of strategy (market-neutral, many coins). See the roadmap in `TODO.MD`.

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

## 2026-09-25: Perp assumptions checked against Kraken Futures' public data

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

## 2026-09-25: Same sweep under perpetual-futures costs

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
