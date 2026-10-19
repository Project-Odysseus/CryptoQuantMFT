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

- [ ] **2.1 Component interfaces.** Typed protocols for `Feature`, `Signal` (forecast in a defined unit, e.g. a
      volatility-normalised score), `Regime` (state probabilities), `Overlay` (signals plus regime to weights),
      `Combiner`, `Sizer`, `PortfolioConstructor`, `ExecutionModel`, `CostModel`. Each declares its warm-up and
      required inputs.
- [ ] **2.2 Vectorised event-time engine.** Panel in (dates × instruments × fields), positions and P&L out.
      - It enforces decision-time alignment: a decision at the close of t trades at the next tradable price (the
        close of t+1 by default; next open optional) and earns from then.
      - It handles different instrument calendars and holidays without look-ahead.
- [ ] **2.3 Execution and cost models:**
      - per-instrument spread, commission and slippage;
      - a market-impact option (square-root of participation);
      - roll costs; funding and financing (futures excess return, perp funding, cash interest);
      - FX conversion to the base currency.
      Cost cases (low/base/high) are first-class.
- [ ] **2.4 Sizing:** volatility targeting (EWMA, HAR), risk parity, inverse-volatility and fixed. Leverage and gross
      caps. Whole contracts or lots, with rounding reported.
- [ ] **2.5 Portfolio construction:** sleeves → instrument targets with netting, budgets, correlation-aware risk
      targeting, exposure limits, and turnover control (bands, buffering). Reuse and generalise
      `src/portfolio/allocation.py` and `risk.py`.
- [ ] **2.6 Attribution built in:** P&L by signal, by instrument, by regime state, long and short, gross and net,
      cost and financing components, and beta to chosen benchmarks.
- [ ] **2.7 Reproducibility check.** Port H14 (and H13) onto the engine. The numbers must match the original studies
      to the reported precision, or the difference must be explained and accepted.
- [ ] **2.8 Performance.** A 30-year, 30-instrument daily panel with 20 signals runs in seconds. Feature caching is
      keyed by data fingerprint and parameters.

## Phase 3: Signals, regimes and overlays as research objects

The regime detector is a signal like any other: it gets researched, scored and validated, and it does not just
switch strategies.

- [ ] **3.1 Signal library.** Port and normalise the existing signals: trend forms (MA cross, EWMAC, breakout,
      `trend_forecast`), mean reversion, carry, relative momentum, volatility, and the crypto signals (funding, taker
      flow). Each gets a docstring stating its hypothesis and its expected sign and horizon.
- [ ] **3.2 Regime models as components:**
      - Feature-score blends (the H14 overlay).
      - Single-feature thresholds.
      - Hidden Markov / Markov-switching models (Gaussian HMM on returns and volatility).
      - Volatility-state models.
      - Trend-strength filters.
      - Cross-asset regimes (rates and volatility).
      - Each outputs state probabilities over time, not only labels, and states its decision lag.
- [ ] **3.3 Regime test bench with synthetic ground truth.** A simulator of regime-switching price processes: trend
      stretches, mean-reverting stretches, volatility shocks, structural breaks, configurable durations and noise.
      For every regime model it measures:
      - detection accuracy, precision and recall per state;
      - detection lag;
      - false-switch rate;
      - stability under noise and parameter changes.
      A regime model must show it can detect regimes that exist before it is trusted on real data.
- [ ] **3.4 Regime card (real data).** The signal-card equivalent for a regime model:
      - does the state predict which strategy earns more next (out of sample, with intervals)?
      - persistence and duration statistics;
      - lag after market events;
      - redundancy with the signals it switches between;
      - placebo (shifted states).
- [ ] **3.5 Overlay as component.** Any set of signals plus any regime model gives weights, through blending
      schedules, hysteresis, confidence scaling, stress rules and caps. The benchmarks are computed automatically:
      static blend, hard switch, exposure-matched static, best single signal.
- [ ] **3.6 Conditional evaluation** of every signal by regime state, as a standard report section.

## Phase 4: Validation and statistics

- [ ] **4.1 Power analysis at pre-registration.** Given the history length, the frequency and the hypothesised
      Sharpe or effect, report the detectable effect and the probability of a pass. Warn when a test cannot answer
      its question. H13 and H14 needed this: 27 daily years cannot separate a Sharpe of 0.3 from zero.
- [ ] **4.2 Walk-forward and purged cross-validation.** Expanding and rolling walk-forward with refit schedules;
      combinatorial purged CV with an embargo, for anything fitted; the probability of backtest overfitting
      reported automatically.
- [ ] **4.3 Strategy, signal and regime cards** (extend the existing ones). One standard each, with pass marks in
      versioned files; equity curves and attribution on every card.
- [ ] **4.4 Lab-wide multiple-testing control.** False-discovery-rate control across all hypotheses and their trials
      (Benjamini-Hochberg / Storey), alongside the per-study deflated Sharpe and Romano-Wolf. A lab-level dashboard
      of how many ideas were tried and how many passed.
- [ ] **4.5 Robustness suite:** parameter plateaus (not optima), cost multipliers, start-date and sub-period
      stability, market environments, cross-market transfer with unchanged rules, and episode dependence.
- [ ] **4.6 Scenario and stress testing.** Historical scenarios (1987, 2000-02, 2008, 2020, 2022 rates) and
      hypothetical shocks: volatility doubling, correlations going to one, gaps.
- [ ] **4.7 Holdout governance.** Keep the frozen final holdout. One logged look per candidate, with approval.

## Phase 5: Experiment tracking and reporting

- [ ] **5.1 Experiment store.** A queryable database (SQLite now, Postgres-ready) of every run:
      - hypothesis and version, config, config hash, data snapshots, commit;
      - metrics, card verdicts, artefacts' paths.
      It replaces scattered output folders as the index.
- [ ] **5.2 Hypothesis registry.** Every hypothesis with status (idea, pre-registered, running, passed, failed,
      paper, live, retired), its reports and its decision. One page shows the lab's whole state.
- [ ] **5.3 Standard report generator.** One command or one notebook call gives an HTML or PDF report with:
      - a data card and the strategy definition;
      - equity curves and drawdowns;
      - costs, attribution, regime breakdown, robustness, cards and verdict;
      - a section a portfolio manager can read in five minutes.
- [ ] **5.4 Charts library:** consistent, publication-quality equity, drawdown, rolling, attribution, regime
      shading, heatmaps and parameter surfaces. Works inline in notebooks (the Agg-backend problem must never come
      back) and in exported reports.

## Phase 6: The notebook workflow (the owner's main interface)

- [ ] **6.1 A five-line idea test.**
      ```python
      lab = Lab(universe="equity_index_futures", start="1999")
      idea = lab.signal(lambda px: (px.close.rolling(20).mean() / px.close.rolling(100).mean() - 1).rank(pct=True))
      lab.test(idea)                         # card, curves, costs, attribution, versus the existing signals
      lab.combine(idea, "trend", overlay="h14_regime")  # how it changes the book
      ```
      *Done when* the owner can do exactly this, and every call is recorded in the experiment store as exploratory
      (not a trial) until pre-registered.
- [ ] **6.2 Exploratory versus confirmatory mode.** Exploratory runs are clearly labelled and counted. Confirmatory
      runs require a locked pre-registration and use the frozen-holdout rules.
- [ ] **6.3 "Compare with the book."** Any idea is shown against and inside the current portfolio: correlation,
      marginal Sharpe, marginal drawdown, diversification.
- [ ] **6.4 Templates:** a new-signal notebook, a new-regime-model notebook, an overlay-design notebook (the H14
      walkthrough generalised), a portfolio-construction notebook, and a strategy-report notebook.
- [ ] **6.5 Speed.** Re-running an idea after a change takes seconds, using cached features and data.

## Phase 7: From research to paper to live, at the same standard

- [ ] **7.1 One strategy object** from research is what paper and live run: same components, same config, same
      code. A promotion moves a config and version, nothing is rewritten.
- [ ] **7.2 Promotion gates,** automated and logged:
      - passed cards;
      - power-adequate test;
      - holdout look;
      - capacity and cost check;
      - risk budget;
      - owner sign-off.
      The existing `--require-strategy-cards` becomes part of this.
- [ ] **7.3 Paper/live parity monitor.** Daily comparison of live or paper positions and P&L against the research
      engine replayed on the same data. Flag divergence beyond tolerance (slippage, data, timing).
- [ ] **7.4 Live monitoring against expectation.** Track Sharpe, drawdown, turnover and costs against the strategy's
      shrunk Sharpe and expected ranges; decay alarms; kill criteria (generalise `src/portfolio/review.py`).
- [ ] **7.5 Execution adapters for a fund.**
      - The order-file and desk handover (exists for futures), and FIX or broker API adapters behind one interface.
      - Paper uses the same interface with simulated fills.
      - The crypto runtime is one adapter, not the centre.
- [ ] **7.6 Capacity and implementation shortfall analysis** per strategy, at fund-relevant sizes.
- [ ] **7.7 Documentation for adoption:** a strategy factsheet template (mechanism, evidence, risks, capacity,
      correlations, operational needs) as a fund would want it.

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
