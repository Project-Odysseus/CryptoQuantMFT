# Signal checks: what QuantTAA does, and a plan to bring it into signal research here

Written 2026-10-11. Nothing here is built. Sizes: S = a day or less, M = a few days.

## Purpose

QuantTAA (`/Users/Sakarias/Odysseus/QuantTAA`) judges a signal by how well it predicts the return that follows, and
then spends most of its effort asking whether that number could be luck, a leak or one lucky subsample. This
project judges mostly finished strategies (returns after costs, Sharpe ratio, placebo on positions). The checks
below sit one step earlier: on the signal itself, before any trading rule or cost. Bringing them in gives every new
idea the same first screen, run from one call in a notebook.

## Notes: how QuantTAA checks a signal

Read from the code at commit `84edf7e` and from `IC_AUDIT.md`. QuantTAA was not changed.

| # | Check | How it is done | Where |
|---|---|---|---|
| 1 | Information coefficient (IC) | Spearman rank correlation between the score at a date and the return over the next h periods. One number per model and horizon. | `signals/evaluate.py` `spearman`, `evaluate` |
| 2 | Cross-sectional IC | Per date: rank correlation across models between score and forward return, both relative to that date's average. Plus the return of the top group minus the bottom group. | `models/ranking.py` `_row_spearman`, `rank_group` |
| 3 | Hit rate and spread | Share of dates where the score's sign matches the sign of the return against its average; mean return after positive scores minus after negative ones. | `signals/evaluate.py` `evaluate` |
| 4 | Overlap-aware significance | Forward returns over h periods overlap. The t-statistic of the spread uses Newey-West errors with h − 1 lags. The IC's own t-statistic is IC × sqrt(n / h). | `evaluate.py` `newey_west_tstat`; `models/explain.py` |
| 5 | Effective sample size and interval | Independent observations behind a correlation of two autocorrelated series: n / (1 + 2 Σ acf_score × acf_return), then a 95% interval from the Fisher transform. | `notebooks/bqnt_ic_audit.py` `effective_n`, `interval` |
| 6 | Non-overlapping IC | IC on every h-th date, averaged over the h possible starting points. Shows whether overlap inflates the level (it did not; it only makes it look more precise). | audit notebook, section 2 |
| 7 | Delay test | IC with the score delayed by 1 to 12 extra periods. A real slow signal fades gradually; a timing leak falls off a cliff at the first delay. | audit notebook, section 3 |
| 8 | Placebo on the dates | The score against returns from other dates: random permutations, and circular shifts, which keep the persistence of both series. Gives a p-value. | audit notebook, section 4 |
| 9 | Placebo through the whole pipeline | The target's returns are shuffled in time and the entire model is run again. Any path by which a return leaks into the score that predicts it still shows as a positive IC. | audit notebook, section 4 |
| 10 | Noise benchmark | The whole model is run on synthetic random walks with many seeds. Kept: the mean IC of a model with no skill (a small negative bias), its standard deviation, and the 95th percentile of the best IC among N models. A real IC is then stated as standard deviations above noise. | `signals/noise.py` |
| 11 | Replay | The model is run as of earlier dates and the scores compared with the full run. Later data must not change earlier scores. | audit notebook, section 5 |
| 12 | Stability over time | IC per decade or five-year block, and a rolling IC. | `evaluate.py` `evaluate_by_period`, `rolling_ic` |
| 13 | Concentration | The average IC without the best 3 and 5 models, and without the worst 3. | audit notebook, section 6 |
| 14 | Single inputs | The IC of every single input on its own, as a distribution: how many are large, how large the largest is. With hundreds of inputs some reach 0.5 by luck. | audit notebook, section 7 |
| 15 | Redundancy | Groups of inputs whose rank correlation is 0.8 or more, so ten versions of one series are not ten votes. | `signals/redundancy.py` |
| 16 | Naive baseline | The sign of the target's own trailing 12-period return. A model has to beat it. | `signals/baselines.py` |
| 17 | Point-in-time buckets | A signal's level is ranked against its own history up to that date only, never the full sample. The score at t uses only returns that had fully happened by t (an embargo of h). | `signals/states.py`, `signals/conditional.py` |
| 18 | Shrinkage by sample size | A conditional mean is shrunk by n_eff / (n_eff + k), with n_eff = n / h. | `signals/conditional.py` |
| 19 | Trust label | From the IC, its significance, the months of history and the independent observations: strong, useful, contrary, short history, no data. | `models/explain.py` |
| 20 | Fixed variants | Design variants are written in config before the run and compared side by side; nothing is tuned. | `experiments.py` |

What its audit concluded, which is the reason to copy the method: 25 models, none with an IC above 0.5; four of
100 results had an interval excluding zero when five are expected by chance; the models' spread of ICs was about
the same as the placebo's; the average rested on three models and on one five-year block. Without checks 8, 9, 12
and 13 the same numbers read as "several models with an IC of 0.3 to 0.4".

## What this project already has, and what is missing

| QuantTAA check | Here today | Gap |
|---|---|---|
| 1 IC | `features.information_coefficient` | None for one series |
| 2 Cross-sectional IC | Only inside individual studies | A reusable per-date IC series, its mean and its t-statistic |
| 3 Hit rate and spread | `features.bucket_table` (per bucket) | The sign-based pair with a t-statistic |
| 4 Overlap-aware significance | Newey-West regression in `inference.py`; t-statistics on every h-th bar in `features.py` | Fine; needs joining to the IC |
| 5 Effective sample size, interval | Block bootstrap intervals for means | **Missing for the IC** |
| 6 Non-overlapping IC | One starting point only | Average over all h starting points |
| 7 Delay test | `features.ic_decay` varies the horizon, not the delay | **Missing** |
| 8 Placebo on the dates | Circular shifts on a strategy's positions (`scorecard.placebo_beaten`) | **Missing on the signal** |
| 9 Pipeline placebo | No | **Missing**; matters for fitted models such as `forecast.walk_forward_ridge` |
| 10 Noise benchmark | No | **Missing** |
| 11 Replay | Written by hand in some tests | A reusable helper |
| 12 Stability | `features.ic_by_year` | Rolling IC |
| 13 Concentration | No | **Missing** (by coin, by year) |
| 14 Single inputs | No | **Missing**; the IC-level twin of the trial ledger |
| 15 Redundancy | Sleeve correlations in the book | Missing at the feature level |
| 16 Naive baseline | Buy-and-hold in the scorecard | A baseline for the signal itself |
| 17 Point-in-time buckets | `bucket_table` cuts on the whole sample (its docstring says so) | An expanding version |
| 18 Shrinkage | Ridge in `forecast.py` | Not needed as a check |
| 19 Trust label | Pass, kill or inconclusive, written per pre-registration | A standard verdict for the first screen |
| 20 Fixed variants | Pre-registration and the trial ledger | Already stricter here |

Things this project has that QuantTAA lacks, and that stay as they are: costs against the effect size, the frozen
holdout, pre-registration locks, the trial ledger with the deflated Sharpe ratio, matched controls in event studies.

## Design

### One call in a notebook

```python
from src.research import signal_checks as sc

card = sc.signal_card(signal, close, horizons=[1, 5, 20], name="funding_z")
card.table      # one row per check: the number, what it is compared with, pass or fail
card.verdict    # "worth a strategy test", "weak" or "no evidence", with the reason in one line
card.plot()     # IC by horizon and by delay, rolling IC, IC by year, the placebo's spread with the real IC marked
card.save()     # a markdown page and the tables, next to the other research output
```

- `signal` and `close` are a Series for one market, or a DataFrame of dates by markets. With a DataFrame the card
  reports the cross-sectional IC as well as the per-market one.
- `horizons` are in bars. Forward returns start at the bar after the signal is known, as in `features.py`.
- Nothing else is needed for the first tier. A second call covers the checks that must rerun the signal's own code:

```python
leak = sc.pipeline_checks(build=make_signal, data=bars, horizons=[5])
# build(data) -> signal. Runs it on a shuffled target, as of earlier dates, and on synthetic noise.
```

- For many candidate signals at once:

```python
board = sc.compare({"funding_z": s1, "oi_change": s2, "taker_flow": s3}, close, horizons=[5])
board.table           # every signal's card on one row
board.redundant       # groups that are the same signal in different clothes
board.luck_threshold  # the IC the best of N signals reaches by chance; only above it is the best one interesting
```

### Two tiers, because they need different things

**Tier 1, from the series alone (seconds).** Checks 1 to 8 and 12, 13, 16, 17: IC with an effective sample size
and interval, non-overlapping IC, hit rate and spread with Newey-West errors, the delay profile, permutation and
circular-shift placebos, rolling IC and IC by year, the result without the best market and without the best year,
the naive baseline, expanding buckets. Plus the comparison this project adds: the top bucket's forward return
against a round trip's cost (`features.ic_decay` already does this).

**Tier 2, from the function that builds the signal (minutes).** Checks 9, 10 and 11: the shuffled target through
the pipeline, the noise benchmark, the replay as of earlier dates. A plain feature that never sees the target
passes these trivially; they matter for anything fitted (ridge forecasts, conditional scores, regime models).

**Many signals.** Checks 14 and 15: the distribution of single-signal ICs, the redundancy groups, and the
best-of-N threshold from the placebos.

### Standards fixed in advance

The thresholds live in one file, `research/signal_standard.toml`, not in the notebook, so a verdict cannot be
adjusted after seeing a result. Proposed defaults, to be agreed before the first use:

| Check | Passes when |
|---|---|
| Interval | The IC's 95% interval with the effective sample size excludes zero |
| Placebo | Fewer than 5% of circular shifts give an IC as large |
| Delay | The IC one bar later keeps at least half of its size (otherwise it depends on trading at the signal's own bar) |
| Stability | Same sign in at least 70% of calendar years with enough data |
| Concentration | Same sign without the best market and without the best year |
| Baseline | Larger than the trailing-return baseline's IC |
| Size against cost | The top bucket's forward return is larger than one round trip |
| Sample | At least 30 independent observations at the horizon, else "short history" and no verdict |

Verdict: "worth a strategy test" when all pass, "weak" when the interval and the placebo pass but another fails,
"no evidence" otherwise. A change to the file is a new version, recorded like a pre-registration.

### Where it sits in the process

1. **Idea and pre-registration** (as now).
2. **Signal card** on development data. New. A signal with "no evidence" stops here, before any rule is written.
3. **Strategy test**: rules, costs, the existing scorecard and bootstrap interval (as now).
4. **One look at the frozen 2026 window** with approval (as now).
5. **Paper** (as now).

- The card reads data through the same loaders as every study, so the frozen holdout is cut off automatically.
- Each card is written to the trial ledger: one entry per signal and horizon. `compare` logs every signal in it.
  A card costs trials like any other look, which is the point of check 14.
- A pre-registration can name the card's verdict as its first criterion instead of restating the statistics.

### What differs from QuantTAA and has to be handled

- **Bars, not months.** QuantTAA has about 216 monthly dates per model. Here a signal may have 50,000 hourly bars,
  so a tiny IC is "significant" and useless. The size-against-cost check is therefore part of the verdict.
- **Persistence.** Intraday features are strongly autocorrelated. The permutation placebo is reported but only the
  circular-shift placebo counts, and the effective sample size replaces n / h.
- **Few markets.** With three to forty coins a cross-sectional IC per date is noisy; its mean is tested with
  Newey-West errors, not treated as many independent draws.
- **Speed.** Placebos are vectorised over shifts; the default is 500 shifts.

## Implementation steps

| Step | What | Size | Reuses |
|---|---|---|---|
| C1 | `src/research/signal_checks.py`, tier 1 statistics as small pure functions: `ic`, `ic_by_date`, `effective_n`, `ic_interval`, `non_overlapping_ic`, `hit_and_spread`, `delay_profile`, `placebo`, `rolling_ic`, `ic_by_block`, `leave_out`, `expanding_buckets`, `baseline_ic` | M | `features.forward_return`, `inference.newey_west_ols`, `inference.circular_shifts` |
| C2 | Tests on synthetic data with known answers: pure noise passes each check at about its nominal rate; a planted signal is found; a signal that uses the next bar's return is caught by the delay test | S | |
| C3 | `signal_card`: runs tier 1, applies the standards file, gives the table, the verdict, the plots and the saved page; writes to the ledger | S | `governance.record_trials`, `hypotheses.common.md_table` |
| C4 | `pipeline_checks`: shuffled target, replay as of given dates, noise benchmark from random walks matched to the market's volatility | M | `governance`, the bar loaders |
| C5 | `compare`: one row per signal, redundancy groups, single-signal IC distribution, best-of-N threshold | S | |
| C6 | `notebooks/signal_card.ipynb`: one worked example per kind (one market, a panel, a fitted forecast), and a section in `docs/research_guide.md` saying the card is the first screen | S | |
| C7 | Run the card on three signals whose answer is already known (the taker-flow basket, funding carry H6, the US-morning rule H10) and check the verdicts agree with the research log. Development data only. | S | |

Order: C1 and C2 first, then C3 and C6 so it is usable in a notebook, then C7 as the check that it tells the truth,
then C4 and C5. C1 to C3 with C6 is the useful core and is about three days.

## What to expect

- Most signals will come out as "no evidence" or "weak". That is the result QuantTAA's own audit reached on 25
  models, and it matches this project's research log.
- The checks do not find an edge. They stop time being spent on rules and backtests for a signal that has none,
  and they make the next H-numbered study shorter because its first criterion is already computed.
- With few independent observations no check can pass. The card says "short history" in that case instead of
  giving a verdict.

## Open points for the owner

1. The pass thresholds in the table above: accept them, or change them before the first card is run.
2. Whether a card's "no evidence" should block a pre-registered strategy test, or only be reported next to it.
3. Whether the same card should also be offered to QuantTAA later. It could, since tier 1 needs only two series,
   but the two projects do not share code today.
