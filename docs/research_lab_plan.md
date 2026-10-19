# Research lab plan: a portable, fund-grade research and deployment stack

Written 2026-10-19 with the owner. This is the working to-do list: check items off as they are done, and add a note
when an item turns out wrong or a better way appears. It is written as a brief to a professional quant: each item
says what to build, why, and how we know it is done.

## Goal

Strategies good enough for a multi-asset fund to adopt, taken from idea to paper to live capital through one
pipeline. The pipeline is held to the same standard at every step, so deployment is never the weak link.

**What the owner wants to be able to do.** Open a notebook and:
- write an idea in a few lines;
- test it alone and combined with the existing signals and overlays;
- see the consequences at once: equity curves, risk, costs, overlap with the book, and the significance checks.

**Constraints:**
- **Portable.** The lab must move to another employer's or fund's infrastructure: no hard-coded paths, venues or
  data vendors, and no dependence on this repo's crypto runtime.
- **IP and compliance.** Build only on the owner's own time and equipment, with no employer data, code or
  confidential information in it. Equity, rates and FX research stays research-only while the current employment
  rule applies; crypto may trade. Personal trading rules apply at any future employer.
- **Daily first, decided at the close.** Every layer must accept a bar interval, so hourly or 4h data works later
  without rewrites.
- **The research standard does not relax on the way to production.** The objects a study validates are the objects
  that trade. Nothing is re-implemented for paper or live.
- **Lab-wide progression to state of the art in phases.** Each phase ends with a real hypothesis run through it; no
  phase is built only for its own sake.

## Design principles (apply to every item)

1. **One engine.** Exactly one implementation each of signals, overlays, sizing, portfolio construction, execution
   timing, costs and P&L. Research, paper and live call it. No study writes its own backtest loop (H11, H13 and H14
   each did; that ends here).
2. **Composable components with typed interfaces:** Feature → Signal → Overlay/Regime → Combiner → Sizer → Portfolio
   → Execution → Costs. Each is swappable and testable alone.
3. **Causal by construction.** Everything is computed on data up to the decision time; trades happen after it. The
   engine enforces this, not each author. Tests append future data and check that the past does not change.
4. **Every run is recorded:** config hash, data fingerprint, code commit, results, verdict. Nothing is in a folder
   only.
5. **Statistics decide, prespecified.** Pre-registration, power analysis, trial counting, holdouts and the
   significance cards are part of the workflow, not optional reading.
6. **Asset-agnostic.** Instruments carry their own calendar, currency, contract specification, cost model and data
   source. Crypto, equity index, rates, FX and commodity futures go through the same path.
7. **Readable.** A fund's portfolio manager or a new colleague should be able to read a strategy's definition and
   its report without reading the engine.

---

## Phase 0: Foundations and decisions (before code)

- [x] **0.1 Package boundary.** Put the lab in its own package (working name `quantlab`) inside this repo, with no
      imports from the crypto runtime (`src/runtime`, `src/execution`, `src/portfolio` engine). The runtime may import
      the lab, never the reverse.
      *Done when* `quantlab` installs and its tests pass in a fresh environment with only its own dependencies.
      *2026-10-19:* `packages/quantlab/` (src layout, own `pyproject.toml`, editable install in `environment.yml`). `tests/test_quantlab_boundary.py` fails on any host import, undeclared dependency, module-level optional import, machine path or forced matplotlib backend. Its tests pass with the host repo off the import path (`python -I`). A truly fresh environment is untested; it needs a download, so it waits for wifi.
- [x] **0.2 Dependency policy.** Keep to pandas, numpy and scipy, plus matplotlib or plotly for charts. Optional
      extras (statsmodels, arch, polars) only behind feature flags. Record versions in a lock file.
      *2026-10-19:* core numpy/pandas/scipy/matplotlib; extras `test`, `stats`; `requirements.lock` pins tested versions.
- [x] **0.3 Configuration standard.** TOML or YAML configs, validated by typed schemas (pydantic or dataclasses with
      strict keys). A strategy is fully defined by its config plus its component code.
      *2026-10-19:* TOML into frozen dataclasses (`quantlab.config`: `parse`, `load`, `to_dict`, `config_hash`). It reports every problem at once with its path, rejects unknown keys and bool-as-number, and parses nested, optional, `Literal`, tuple and dict fields. Chosen over pydantic to keep the core dependency-free; revisit if schemas grow complex.
- [x] **0.4 Conventions document.**
      - Time: UTC storage, exchange-local session dates, and a timestamp meaning "bar closes at" (decide and enforce
        one).
      - Returns: log returns for features, simple returns for P&L, excess-of-cash for futures.
      - Position units: weight of equity, contracts or notional, and how one converts to another.
      *2026-10-19:* `packages/quantlab/CONVENTIONS.md` plus `quantlab.conventions` (tested).
      - Bars indexed by when their data is final (daily: session date; intraday: close time, UTC).
      - Execution at the next close by default.
      - Log returns for features, simple returns for P&L, futures as excess return.
      - Weight-of-equity positions.
      - Unit-variance forecasts capped at ±2.
      - Regimes as probabilities.
      - Separate databases.
- [x] **0.5 Migration plan.** Decide what is ported from `src/research` and what is retired. List every current
      study whose numbers must be reproducible after the move (H11, H13, H14, and the crypto sleeves' cards).
      *2026-10-19:* `docs/quantlab_migration.md` (port / adapt / keep / retire, module by module) and `packages/quantlab/tests/golden/studies.json` (H11, H13, H14 and the crypto sleeve cards, with tolerances).
- [x] **0.6 Compliance note** in the repo: what the lab contains, that it holds no employer material, and what may be
      traded where.
      *2026-10-19:* `packages/quantlab/COMPLIANCE.md`.

## Phase 1: Data layer

- [x] **1.1 Instrument master.** One table and schema for every instrument:
      - id, asset class, venue, currency, calendar, multiplier and tick;
      - contract rules (expiries, roll rule), trading hours, cost model reference;
      - data sources.
      Merge `config/futures_markets.csv` and the research market tables into it.
      *2026-10-19:* `quantlab.data.instruments` (Instrument, ContractSpec, InstrumentMaster: strict TOML, proxy links checked). `config/instruments.toml`, 91 instruments generated by `scripts/lab/build_instrument_master.py` from the host tables, with host ids kept. `tests/test_lab_instrument_master.py` keeps it in step with `config/futures_markets.csv`; the futures runtime still reads the CSV.
- [x] **1.2 Data adapters behind one interface:**
      - `load(instrument, fields, start, end, interval) -> frame`, with point-in-time semantics.
      - Adapters: Yahoo (free proxies), OECD and Norges Bank rates, Kraken, Binance archive, and a CSV/Parquet
        "bring your own" adapter for a future employer's data (Bloomberg/Refinitiv exports, Databento).
      - No study calls a vendor directly.
      *2026-10-19:* `quantlab.data.adapters` registry; built in: `yahoo`, `oecd`, `norges_bank`, `file` (CSV/Parquet you bring). The host's `src/lab_adapters` registers `kraken` (cached perp candles, re-stamped by close) and `contracts` (the per-contract recorder's files). Binance archive: adapt when a study needs it.
- [x] **1.3 Dataset versioning.** Every load produces a fingerprinted snapshot (hash of content and source/version),
      stored under a data catalog. A study records the snapshots it used, and reruns use the same snapshot unless
      told otherwise.
      *2026-10-19:* `quantlab.data.catalog.DataCatalog`. Each snapshot is a fingerprinted CSV plus a manifest line; loads are reproducible by snapshot id; a refresh stores a new version only if content changed. The frozen holdout (from 2026-01-01) is cut unless `unlock(reason)` is called, and every unlocked load is logged.
- [x] **1.4 Futures construction done properly.**
      - Per-contract storage; held-contract returns (trading P&L from the contract actually held, rolls as trades with
        costs).
      - Back-adjusted and ratio-adjusted series for signals only, with the adjustment method recorded.
      - Index proxies are allowed, but flagged as proxies in every report.
      *2026-10-19:* `quantlab.data.futures`: contract months, last-trade rules on exchange calendars, roll days, roll schedules, `held_returns` (held contract's own closes, a roll is a round-trip cost, never a gap) and `adjusted_series` (ratio or difference, for signals only). Tested on synthetic contango. Real contract history exists only from 2026 (the recorder), inside the holdout, so index proxies stay flagged as proxies until then.
- [x] **1.5 Data quality suite.** Calendar gaps, duplicates, OHLC consistency, close-only history, stale prints,
      outliers, roll jumps. It runs on every load and produces a data card, which a study must include.
      *2026-10-19:* `quantlab.data.quality.data_card`: duplicates, order, OHLC consistency, close-only years leading to `usable_from`, stale runs, outliers, missing sessions against exact calendars (long gaps otherwise), roll jumps. On real data it agrees with H14's checks (DAX usable from 1994) and found 82 missing S&P 500 sessions in Yahoo's history.
- [x] **1.6 Universe definitions, point-in-time.** Which instruments were tradable and liquid on each date (listing
      dates, minimum volume).
      *2026-10-19:* `quantlab.data.panel.universe` (history length, not stale, optional trailing median volume). Listing dates come from the data's first bar.
- [x] **1.7 Frequency-agnostic bars.** One bar schema with an interval field, and resampling with correct session
      boundaries, so a daily study runs on 4h data by changing one setting.
      *2026-10-19:* `quantlab.data.bars`: schema validation (daily by naive session date, intraday by UTC close), `from_open_stamped`, `to_daily` with a session cut-off in the market's timezone, `available_at`.
- [x] **1.8 Macro and cross-asset inputs** (rates, curves, FX, volatility indices, carry) through the same interface,
      for regime and carry research.
      *2026-10-19:* OECD rates (with `available_at` = the next month) and Norges Bank FX through the same adapters, and `asof_daily` for point-in-time daily series. Volatility indices and curves: add adapters when needed.

## Phase 2: The research engine

- [x] **2.1 Component interfaces.** Typed protocols for `Feature`, `Signal` (forecast in a defined unit, e.g. a
      volatility-normalised score), `Regime` (state probabilities), `Overlay` (signals plus regime to weights),
      `Combiner`, `Sizer`, `PortfolioConstructor`, `ExecutionModel`, `CostModel`. Each declares its warm-up and
      required inputs.
      *2026-10-19:* `quantlab.engine.components`: Signal, PanelSignal, RegimeModel protocols; `signal(name, fn)` for notebook lambdas; `Targets` (target + parts + diagnostics); `per_instrument`, `apply_signal`, `combine_targets`. Overlays and combiners are strategies returning Targets; separate Overlay/Combiner protocols wait until a second overlay family needs them (Phase 3).
- [x] **2.2 Vectorised event-time engine.** Panel in (dates × instruments × fields), positions and P&L out.
      - It enforces decision-time alignment: a decision at the close of t trades at the next tradable price (the
        close of t+1 by default; next open optional) and earns from then.
      - It handles different instrument calendars and holidays without look-ahead.
      *2026-10-19:* `quantlab.engine.backtest.run_backtest`. Size, limits, band (`band_exits_to_zero` is a setting), then decide at the close of t, trade at the close of t+1, earn from t+2, enforced by the engine. Different calendars align in the panel (stale bars carry). NEXT_OPEN raises NotImplementedError until a study needs it.
- [x] **2.3 Execution and cost models:**
      - per-instrument spread, commission and slippage;
      - a market-impact option (square-root of participation);
      - roll costs; funding and financing (futures excess return, perp funding, cash interest);
      - FX conversion to the base currency.
      Cost cases (low/base/high) are first-class.
      *2026-10-19:* per-side bps (fixed, per instrument or per bar), `cost_table` from the master (ticks to bps as the price moves), rolls, cost multiplier, optional square-root impact. `engine.returns.excess_returns` builds excess returns from the master (carry minus point-in-time short rate). Perp funding and FX translation of P&L remain: book them when a crypto or non-base-currency study needs them.
- [x] **2.4 Sizing:** volatility targeting (EWMA, HAR), risk parity, inverse-volatility and fixed. Leverage and gross
      caps. Whole contracts or lots, with rounding reported.
      *2026-10-19:* `Sizer` (EWMA vol targeting with an optional scale cap, or none). HAR and inverse-vol sizing go through allocation for now. `portfolio.construction.to_contracts` gives whole contracts and flags rounding to zero.
- [x] **2.5 Portfolio construction:** sleeves → instrument targets with netting, budgets, correlation-aware risk
      targeting, exposure limits, and turnover control (bands, buffering). Reuse and generalise
      `src/portfolio/allocation.py` and `risk.py`.
      *2026-10-19:* `portfolio.construction.build_portfolio` (sleeves, causal refitted allocation, netting), with the host's allocation math ported (`portfolio.allocation`: fixed, equal, inverse_vol, risk_parity, hrp). `Limits` (gross, net, per-instrument). Correlation-aware risk targeting (the host's risk model) is still to port.
- [x] **2.6 Attribution built in:** P&L by signal, by instrument, by regime state, long and short, gross and net,
      cost and financing components, and beta to chosen benchmarks.
      *2026-10-19:* `analytics.performance`: metrics (the H14 table), trades, attribution by instrument, side, target part and costs, and `benchmark_stats`. Attribution by regime state reads `Targets.diagnostics`.
- [x] **2.7 Reproducibility check.** Port H14 (and H13) onto the engine. The numbers must match the original studies
      to the reported precision, or the difference must be explained and accepted.
      *2026-10-19:* `tests/test_lab_golden.py`. H14 on the engine from its cached data reproduces the golden numbers: 5A 0.1941, 6A 0.2870, 2A -0.1208, 7A 0.3702, turnover 7.763 and 4.097 exact, regime shares, the exposure-matched and volatility-managed baselines. H13's forecast book reproduces too. H11 (weekly covariance-scaled book) is not ported yet.
- [x] **2.8 Performance.** A 30-year, 30-instrument daily panel with 20 signals runs in seconds. Feature caching is
      keyed by data fingerprint and parameters.
      *2026-10-19:* 20 signals x 30 instruments x 30 years run in about 8 s (tested under 20 s). `engine.cache.cached` memoises features on data content plus parameters (used for H14's regime scores).

## Phase 3: Signals, regimes and overlays as research objects

The regime detector is a signal like any other: it gets researched, scored and validated, and it does not just
switch strategies.

- [x] **3.1 Signal library.** Port and normalise the existing signals: trend forms (MA cross, EWMAC, breakout,
      `trend_forecast`), mean reversion, carry, relative momentum, volatility, and the crypto signals (funding, taker
      flow). Each gets a docstring stating its hypothesis and its expected sign and horizon.
      *2026-10-19:* `quantlab.signals.library`: MA cross, EWMAC, breakout, slope t-stat, `trend_forecast`, H14's trend, z-score reversion, short reversal, time-series momentum, low volatility, ATR channel, cross-sectional `relative_momentum`; a causal `normalize`; each with hypothesis, sign and horizon; tested causal and on the forecast scale. Carry and the crypto signals (funding, taker flow) wait for their data in the lab catalog.
- [x] **3.2 Regime models as components:**
      - Feature-score blends (the H14 overlay).
      - Single-feature thresholds.
      - Hidden Markov / Markov-switching models (Gaussian HMM on returns and volatility).
      - Volatility-state models.
      - Trend-strength filters.
      - Cross-asset regimes (rates and volatility).
      - Each outputs state probabilities over time, not only labels, and states its decision lag.
      *2026-10-19:* `quantlab.regimes.models`: `FeatureBlendRegime` (H14, hard or soft), `PercentileRegime` (any feature or callable, soft or hard with hysteresis and confirmation; trend-strength filters are this on ER, |slope t| or ADX), `VolatilityRegime`, `HMMRegime` (`regimes.hmm`: own Baum-Welch, filtered probabilities only, refitted yearly on the past, states named by volatility). All return probabilities, declare a lag, and are tested not to change with later data. Cross-asset regimes (rates, implied vol) are a `PercentileRegime` on a cross-asset feature once that data is in the catalog.
- [x] **3.3 Regime test bench with synthetic ground truth.** A simulator of regime-switching price processes: trend
      stretches, mean-reverting stretches, volatility shocks, structural breaks, configurable durations and noise.
      For every regime model it measures:
      - detection accuracy, precision and recall per state;
      - detection lag;
      - false-switch rate;
      - stability under noise and parameter changes.
      A regime model must show it can detect regimes that exist before it is trusted on real data.
      *2026-10-19:* `regimes.simulate` (TREND / RANGE / SHOCK episodes, noise, structural break) and `regimes.bench` (`bench`, `sweep`). First result: no detector is good. Balanced accuracy 0.57-0.60 for the single features, 0.45 for H14's blend (it abstains 30% of the time and confirms 13 bars late), 0.53 for the HMM (it finds volatility, not trend).
- [x] **3.4 Regime card (real data).** The signal-card equivalent for a regime model:
      - does the state predict which strategy earns more next (out of sample, with intervals)?
      - persistence and duration statistics;
      - lag after market events;
      - redundancy with the signals it switches between;
      - placebo (shifted states).
      *2026-10-19:* `validation.regime_card` (edge with block-bootstrap interval, shifted-label placebo, spells, lag after breakouts, redundancy; `RegimeStandard` v1). On NDX, trend vs 40-day reversion: H14's blend, ER, |slope t| and ADX show no evidence. The volatility state and the HMM pass at p 0.040 and 0.044, which six trials can produce by chance, so they are candidates for a pre-registered test, not findings.
- [x] **3.5 Overlay as component.** Any set of signals plus any regime model gives weights, through blending
      schedules, hysteresis, confidence scaling, stress rules and caps. The benchmarks are computed automatically:
      static blend, hard switch, exposure-matched static, best single signal.
      *2026-10-19:* `overlays.overlay.RegimeOverlay` (soft blend or hard switch, gross cap, cap) and `benchmarks` (static blend, hard switch, exposure-matched static, each signal alone). Confidence scaling and stress rules remain inside H14's strategy until a second overlay needs them.
- [x] **3.6 Conditional evaluation** of every signal by regime state, as a standard report section.
      *2026-10-19:* `analytics.conditional.by_regime` and `after_changes`. Notebook: `notebooks/regime_lab.ipynb` covers all of Phase 3.

## Phase 4: Validation and statistics

- [x] **4.1 Power analysis at pre-registration.** Given the history length, the frequency and the hypothesised
      Sharpe or effect, report the detectable effect and the probability of a pass. Warn when a test cannot answer
      its question. H13 and H14 needed this: 27 daily years cannot separate a Sharpe of 0.3 from zero.
      *2026-10-19:* `validation.power` (Sharpe standard error, pass probability under the t >= 2 gate and with trials, detectable Sharpe, years needed, detectable IC, `power_report` with an underpowered warning), checked against simulation. NDX's 41 years detect a Sharpe of 0.44 at 80% power; a true 0.3 passes 47% of the time.
- [x] **4.2 Walk-forward and purged cross-validation.** Expanding and rolling walk-forward with refit schedules;
      combinatorial purged CV with an embargo, for anything fitted; the probability of backtest overfitting
      reported automatically.
      *2026-10-19:* `validation.crossval`: expanding and rolling walk-forward splits, purged k-fold with embargo, CPCV splits and path recombination, `select_walk_forward` (pick on train, trade on test, with PBO). H14's trend grid on NDX: walk-forward Sharpe 0.36 vs 0.38 best in hindsight, PBO 0.73 (the grid is a plateau, so picking adds nothing).
- [x] **4.3 Strategy, signal and regime cards** (extend the existing ones). One standard each, with pass marks in
      versioned files; equity curves and attribution on every card.
      *2026-10-19:* the host's strategy and signal cards ported to `validation.strategy_card` / `validation.signal_card` (statistics unchanged), regime card from Phase 3; pass marks in versioned `validation/standards/{strategy,signal,regime}.toml`. `card_backtest` cards an engine result with the timing placebo and attribution charts (by part, by instrument, against the baseline). The host keeps its own cards for `--portfolio-check` until the paper books move.
- [x] **4.4 Lab-wide multiple-testing control.** False-discovery-rate control across all hypotheses and their trials
      (Benjamini-Hochberg / Storey), alongside the per-study deflated Sharpe and Romano-Wolf. A lab-level dashboard
      of how many ideas were tried and how many passed.
      *2026-10-19:* `validation.ledger` (the lab's own ledger: trials, card notes, pre-registration locks) and `validation.fdr` (Benjamini-Hochberg, Storey with at least 20 cards, `lab_fdr`, `dashboard`, `ledger.lab_summary`). A strategy card's p is the larger of its t-test p and its timing placebo p, so drift alone is no discovery. First run: of seven cards (six regime detectors, H14's trend), none survives at FDR 10%.
- [x] **4.5 Robustness suite:** parameter plateaus (not optima), cost multipliers, start-date and sub-period
      stability, market environments, cross-market transfer with unchanged rules, and episode dependence.
      *2026-10-19:* `validation.robustness`: parameter plateau (neighbourhood ratio, sweep for PBO), cost sensitivity and break-even, start dates, sub-periods, bull/bear and calm/volatile environments, cross-market transfer, best-months removal and best-day concentration.
- [x] **4.6 Scenario and stress testing.** Historical scenarios (1987, 2000-02, 2008, 2020, 2022 rates) and
      hypothetical shocks: volatility doubling, correlations going to one, gaps.
      *2026-10-19:* `validation.scenarios`: twelve named crises (1987 to the 2022 rates shock) against a benchmark; volatility x2, correlations to one and n-sigma gaps on the book's own positions.
- [x] **4.7 Holdout governance.** Keep the frozen final holdout. One logged look per candidate, with approval.
      *2026-10-19:* `ledger.holdout_cut` cuts from 2026-01-01 on every card; `ledger.holdout_look(candidate, approved_by=...)` opens it once per candidate, logged; a second look needs `again=<reason>`. Notebook for Phase 4: `notebooks/validation_lab.ipynb`.

## Phase 5: Experiment tracking and reporting

- [x] **5.1 Experiment store.** A queryable database (SQLite now, Postgres-ready) of every run:
      - hypothesis and version, config, config hash, data snapshots, commit;
      - metrics, card verdicts, artefacts' paths.
      It replaces scattered output folders as the index.
      *2026-10-19:* `quantlab.tracking.store.ExperimentStore`, SQLite at `quantlab_data/research/lab.db` (plain SQL; any DB-API connection via `connect=`). `store.run(...)` records config and hash, snapshots, commit, state (failed runs keep the error); a run logs metrics, results, cards, series, figures and artefacts. Queries: `runs`, `get`, `metrics`, `series`, `compare` (with config differences).
- [x] **5.2 Hypothesis registry.** Every hypothesis with status (idea, pre-registered, running, passed, failed,
      paper, live, retired), its reports and its decision. One page shows the lab's whole state.
      *2026-10-19:* statuses idea, pre-registered, running, passed, inconclusive (added: most host studies ended there), failed, paper, live, retired; every change needs a reason and is kept (`history`); `lab_state()` is the one-page view, active work first. `scripts/lab/seed_registry.py` seeds H1-H14 from `research/prereg` and the reports' verdicts.
- [x] **5.3 Standard report generator.** One command or one notebook call gives an HTML or PDF report with:
      - a data card and the strategy definition;
      - equity curves and drawdowns;
      - costs, attribution, regime breakdown, robustness, cards and verdict;
      - a section a portfolio manager can read in five minutes.
      *2026-10-19:* `quantlab.reporting.report.build_report`: one self-contained HTML file (charts embedded; print to PDF from a browser) with a five-minute summary (verdict, definition, headline numbers, what can go wrong, recommendation by verdict), definition and config, data cards, performance, costs and attribution, regimes, robustness, scenarios and stress, the strategy card, reproducibility. Anything not given is computed.
- [x] **5.4 Charts library:** consistent, publication-quality equity, drawdown, rolling, attribution, regime
      shading, heatmaps and parameter surfaces. Works inline in notebooks (the Agg-backend problem must never come
      back) and in exported reports.
      *2026-10-19:* `quantlab.reporting.charts`: equity, drawdown, rolling Sharpe, attribution by part or instrument, regime shading, heat maps, parameter surfaces, monthly returns, tearsheet; one style; PNG/base64 export. A test fails if any lab module selects a matplotlib backend. Notebook: `notebooks/lab_record.ipynb`.

## Phase 6: The notebook workflow (the owner's main interface)

- [x] **6.1 A five-line idea test.**
      ```python
      lab = Lab(universe="equity_index_futures", start="1999")
      idea = lab.signal(lambda px: (px.close.rolling(20).mean() / px.close.rolling(100).mean() - 1).rank(pct=True))
      lab.test(idea)                         # card, curves, costs, attribution, versus the existing signals
      lab.combine(idea, "trend", overlay="h14_regime")  # how it changes the book
      ```
      *Done when* the owner can do exactly this, and every call is recorded in the experiment store as exploratory
      (not a trial) until pre-registered.
      *2026-10-19:* `quantlab.lab.Lab`: `Lab(universe=..., start=...)`, `lab.signal(fn)` (a function of `px`, a library signal, or per-instrument bars), `lab.test`, `lab.combine(..., overlay=...)`, `lab.compare_with_book`, `lab.report`. Every signal is checked for hindsight: the plan's own example (`.rank(pct=True)` down each column) is caught as using later data; `.rank(axis=1, pct=True)` passes. Real data comes from the catalog, filled offline by `scripts/lab/import_research_cache.py`; notebooks find the project root from their own folder.
- [x] **6.2 Exploratory versus confirmatory mode.** Exploratory runs are clearly labelled and counted. Confirmatory
      runs require a locked pre-registration and use the frozen-holdout rules.
      *2026-10-19:* without `hypothesis=` every call is a run under EXPLORATORY in the store (counted, `lab.exploration_count()`), with no trials and no ledger cards. With `hypothesis=`, the pre-registration must be locked, tests log trials and cards to that family, and `lab.final_look(approved_by=...)` opens the frozen holdout once.
- [x] **6.3 "Compare with the book."** Any idea is shown against and inside the current portfolio: correlation,
      marginal Sharpe, marginal drawdown, diversification.
      *2026-10-19:* `lab.compare_with_book`: correlation with the book and each member, marginal Sharpe and drawdown at one member's risk, diversification ratio, tangency risk share; the book is BOOK's signals at equal risk, or any P&L given.
- [x] **6.4 Templates:** a new-signal notebook, a new-regime-model notebook, an overlay-design notebook (the H14
      walkthrough generalised), a portfolio-construction notebook, and a strategy-report notebook.
      *2026-10-19:* `notebooks/templates/`: new_signal, new_regime_model, overlay_design, portfolio_construction, strategy_report; each runs end to end on the cached data in 2-7 s.
- [x] **6.5 Speed.** Re-running an idea after a change takes seconds, using cached features and data.
      *2026-10-19:* a full `lab.test` (both cards, book comparison, cost doubling) on 7 indices x 27 years takes about 3 s; book signals and backtests are kept per lab; features cached (Phase 2). Tested under 10 s.

## Phase 7: From research to paper to live, at the same standard

- [x] **7.1 One strategy object** from research is what paper and live run: same components, same config, same
      code. A promotion moves a config and version, nothing is rewritten.
      *2026-10-19:* `quantlab.deploy.spec`: a strict, versioned, hashed spec file (`research/strategies/smooth_trend.toml` is the H13 design). The host runs it as `strategy = "lab"` with `params = { spec, version }` (`src/lab_adapters/strategy.py`); the sleeve's signal equals the host's own `trend_forecast` bar for bar (tested), and a pinned version the file no longer has is refused.
- [x] **7.2 Promotion gates,** automated and logged:
      - passed cards;
      - power-adequate test;
      - holdout look;
      - capacity and cost check;
      - risk budget;
      - owner sign-off.
      The existing `--require-strategy-cards` becomes part of this.
      *2026-10-19:* `quantlab.deploy.gates`: `evaluate` (backtest, card, power, 2x costs, capacity), `promotion_check` (cards, power, holdout, costs, capacity, risk budget, sign-off; paper and live requirements differ), `sign_off`, `promote` (overrides need a written reason; sign-off cannot be overridden), `promoted`. All logged to the lab ledger. `--portfolio-check --require-strategy-cards` now also requires a promotion with the current hash for lab sleeves. First run, smooth_trend: paper blocked by its card (no evidence, t 1.04) and its drawdown budget; live also by power (17%) and the holdout.
- [x] **7.3 Paper/live parity monitor.** Daily comparison of live or paper positions and P&L against the research
      engine replayed on the same data. Flag divergence beyond tolerance (slippage, data, timing).
      *2026-10-19:* `quantlab.deploy.monitor.parity` (sign agreement, size ratio, correlation per instrument; daily P&L tracking error; flags with likely causes) and `scripts/lab/parity_check.py` on the book's SQLite snapshots. Monitoring reads the frozen holdout through `operational_lab`, logged as operational access. The equity-trend book has one day of snapshots so far.
- [x] **7.4 Live monitoring against expectation.** Track Sharpe, drawdown, turnover and costs against the strategy's
      shrunk Sharpe and expected ranges; decay alarms; kill criteria (generalise `src/portfolio/review.py`).
      *2026-10-19:* `quantlab.deploy.monitor.expectation_check`: Sharpe against the shrunk Sharpe in standard errors (decay), floor after a year, drawdown against the backtest's worst, volatility, turnover and costs bands; statuses ok / watch / flag / too early; never acts by itself. Expectations live in the spec.
- [x] **7.5 Execution adapters for a fund.**
      - The order-file and desk handover (exists for futures), and FIX or broker API adapters behind one interface.
      - Paper uses the same interface with simulated fills.
      - The crypto runtime is one adapter, not the centre.
      *2026-10-19:* `quantlab.deploy.execution`: `plan_orders` (whole contracts, band, exits first), the `ExecutionAdapter` interface, `PaperAdapter` and `OrderFileAdapter`. Broker/FIX adapters are written against a firm's API behind this interface; the host's Kraken runtime stays the host's adapter.
- [x] **7.6 Capacity and implementation shortfall analysis** per strategy, at fund-relevant sizes.
      *2026-10-19:* `quantlab.deploy.capacity`: `capacity_curve` (Sharpe, impact cost and participation by capital with square-root impact; capacities where the Sharpe halves and where participation hits the limit) and `implementation_shortfall` from fills. smooth_trend: capacity about 1bn USD, set by STOXX 600 futures volume (an assumption in the spec).
- [x] **7.7 Documentation for adoption:** a strategy factsheet template (mechanism, evidence, risks, capacity,
      correlations, operational needs) as a fund would want it.
      *2026-10-19:* `quantlab.reporting.factsheet.build_factsheet` (mechanism, evidence, risks, capacity, correlations, operational needs, status); `notebooks/templates/promotion.ipynb` walks evidence, gates, factsheet and the decision.

## Phase 8: Engineering quality

- [ ] **8.1 Test pyramid.** Unit tests per component; property tests for causality (append-future-data invariance,
      shifting invariance); golden-number tests for ported studies; an integration test from data to card.
- [ ] **8.2 CI.** Tests, lint and type checks on every commit (locally or GitHub Actions; the owner pushes).
- [ ] **8.3 Typed, documented public API** with docstrings stating why, as the repo's standards require.
- [ ] **8.4 Packaging.** Installable (`pip install -e quantlab`); versioned releases; a changelog.
- [ ] **8.5 Environment portability:** a conda or uv environment file; it runs on macOS and Linux, on a laptop or a
      server; no secrets in code.

---

## First research programmes to run through the lab (one per phase, as acceptance tests)

1. **After Phase 2:** H14 reproduced on the engine. The numbers match.
2. **After Phase 3:** the regime-detector programme.
   - Synthetic bench first: which regime models detect known regimes, and with what lag?
   - Then regime cards on real data for the survivors.
   - Then an overlay pre-registration (H15) built on the best detector.
3. **After Phase 4:** re-judge the running crypto sleeves with power analysis and walk-forward. Do the current paper
   books deserve their capital?
4. **After Phase 6:** the owner tests a new idea end to end in the notebook in under 15 minutes.
5. **After Phase 7:** one strategy promoted from research to paper through the gates, with the parity monitor live.

## Open questions (to settle as we go)

- ~~Plotly or matplotlib~~ **matplotlib** (owner, 2026-10-19).
- ~~Port versus wrap~~ **Refactor for reuse:** port where the logic is host-agnostic, wrap or adapt where it is
  host-specific (owner, 2026-10-19; detail in `docs/quantlab_migration.md`).
- ~~Same or separate database~~ **Separate** (owner, 2026-10-19).
- Which external data is worth paying for, if any, and only for crypto while the employment rule stands.
- Licensing of the code itself (a private repo; the owner keeps the rights; no employer contributions).
