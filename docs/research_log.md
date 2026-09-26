# Research log

Dated findings from strategy research, newest first (entries from the same day too). Methodology and column
meanings are in [`research_guide.md`](research_guide.md). Record failures too, so they don't get re-tested by
accident.

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
