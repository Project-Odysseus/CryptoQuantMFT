# Hypothesis research: H1 crowding, H2 liquidation overshoot, H3 variance risk premium

Pre-registered tests of three mechanism-driven hypotheses on BTC, ETH and SOL (spot and USDT perps), run
2026-09-28 on development data to 2025-12-31. The frozen holdout (2026-01-01 on) was opened once, with approval,
for the only development survivor (H1b carry); it failed.

| | Hypothesis | Verdict (development) | Report |
| --- | --- | --- | --- |
| H1a | High funding z (crowded longs) predicts lower 1-7d returns | **FAIL**: the sign is reversed; tilt fails placebo | [H1](reports/H1.md) |
| H1b | Timed delta-neutral carry clears 4 legs of costs | Passed development at Binance fees; **FAILED the holdout** (-0.10% Jan-Aug 2026: funding stayed far below the hurdle) | [H1](reports/H1.md) |
| H2 | Liquidation cascades revert (PROXY events) | **FAIL** (proxy): indistinguishable from vol-matched baseline, loses at stressed costs | [H2](reports/H2.md) |
| H3-P1 | IV exceeds later realized vol | **FAIL as registered**: yes for BTC (+6 pts, t 3.4), no for ETH | [H3](reports/H3.md) |
| H3-A1 | IV sizing beats realized-vol sizing | **FAIL** (killed): +0.02 Sharpe, worse matched drawdown | [H3](reports/H3.md) |
| H3-A2 | Risk-off on term-structure inversion | **UNTESTED** (no history); a labelled DVOL-spike proxy hurt | [H3](reports/H3.md) |

Trials logged to `data/research/trial_ledger.jsonl`: H1 12, H2 3, H3 4 (ledger total 1,199 with the backfill).

## What this means for deployment

1. **Nothing is ready to trade.** The one development survivor, timed carry, earned nothing in the holdout: it only
   pays when funding runs well above ~10% a year (2020-21, 2024). On Kraken its 0.40% spot fee makes it marginal
   even then. Treat it as a regime trade to revisit if funding rises, not as a sleeve to build now.
2. **Keep the runtime's EWMA sizing.** IV sizing didn't earn its complexity (H3-A1), and a DVOL-spike risk-off
   filter hurt.
3. **Don't build a funding-crowding filter on the trend book.** That roadmap item assumed the contrarian sign,
   which this test reverses (high funding went with continued trends).

## Suggested next steps, in order

1. ~~Holdout look for H1b~~ done 2026-09-28: failed. In development carry was uncorrelated with the trend book (-0.06)
   and lifted its Sharpe from 0.73 to 0.86 (`scripts/research/combine_sleeves.py`), so it is worth revisiting in a
   high-funding regime; building it would need a low-fee spot + perp venue and a two-leg sleeve type.
2. **Keep the collectors running** (now a LaunchAgent: `deploy/launchd/com.cryptoquant.collectors.plist`) (`scripts/collectors/run_collectors.sh`, liquidations + hourly option chains),
   ideally on the always-on machine. They are the only route to testing H2 on real liquidations (6-12 months) and
   H3's term structure and phase-B bid/ask economics (3-12 months). Phase B (short BTC vol) is the most promising
   lead here: the BTC premium is consistent, but it shrank to 1.7 points in 2025 and has to beat Deribit's spreads.
3. **Add CPI release dates** to `research/data/` by hand (BLS blocked scripts) to complete H2's news split later.

## Layout and how to rerun

| Path | What |
| --- | --- |
| `research/prereg/H{1,2,3}.txt` | Pre-registrations (committed before any result) |
| `research/reports/H{1,2,3}.md` | Reports in the 8-section format |
| `research/coverage.md`, `research/data/` | Data coverage; FOMC calendar |
| `src/research/pit.py` | Point-in-time loaders with `available_at` and the holdout lock (`HoldoutLocked`) |
| `src/research/costs.py`, `bar_engine.py`, `inference.py` | Venue costs, next-open engine with intrabar stops and funding, HAC/bootstrap/placebo/DSR |
| `src/research/hypotheses/` | H1-H3 signals and rules |
| `scripts/research/h{1,2,3}_study.py`, `combine_sleeves.py` | The studies (outputs under `data/research/`) |
| `tests/test_research_harness.py`, `tests/test_research_hypotheses.py` | Holdout-lock, look-ahead and engine tests |

```bash
python -c "from src.research.pit import update_hypothesis_data; update_hypothesis_data()"   # ~250 MB, resumable (wifi)
python scripts/research/h1_study.py     # add --no-ledger when re-running an already logged configuration
python scripts/research/h3_study.py
python scripts/research/h2_study.py
python scripts/research/combine_sleeves.py
```

The holdout opens only inside `governance.final_holdout("reason")` (or `CQM_FINAL_HOLDOUT=reason`), and every look
is logged. Run it once per candidate, and only with the owner's approval.

Deviations from the original brief: the trial ledger is the repo's existing JSONL ledger (tagged by hypothesis)
rather than a new Parquet file; the holdout is the repo's existing boundary (2026-01-01, ~9 months) rather than 12
months, as agreed; and "the per-trade Parquet log" doesn't exist. Per-trade tax records are in SQLite via
`TradeLogger`, which is what any deployment must write to.
