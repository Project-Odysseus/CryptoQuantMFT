# quantlab migration plan (plan item 0.5)

Written 2026-10-19. This plan says how the existing research code becomes the lab without breaking anything that
runs today: the paper and live books, the studies, the notebooks. It is meant to be read alongside
`docs/research_lab_plan.md` and `packages/quantlab/CONVENTIONS.md`.

## The four treatments

| Treatment | Meaning | Rule |
| --- | --- | --- |
| **Port** | The logic is host-agnostic math or statistics. It moves into `quantlab`, generalised to the conventions. | The old host module becomes a thin wrapper that imports from `quantlab`, so every current caller keeps working. Delete the wrapper only when nothing uses it. |
| **Adapt** | Host-specific input/output (Kraken caches, the Binance archive, the trading database). It stays in the host. | It is exposed to the lab through a quantlab interface (for example a data adapter) implemented in a host package, `src/lab_adapters/`. quantlab defines the interface and never imports the implementation. |
| **Keep** | The trading runtime (paper/live engine, books, execution, tax). | It stays in the host for now. In Phase 7 it consumes quantlab strategy objects. |
| **Retire** | Superseded by the one engine. | It stays until the golden numbers below reproduce on the engine, then it is deleted with its tests. |

**Refactoring order:** port the pure pieces first (they carry the tests with them), then build the engine on them, then
reproduce the golden numbers, then retire. Never port and rewrite in the same commit: a port changes the location,
and behaviour changes go in separate commits so a diff shows them.

## Module by module

### Statistics and validation: port (Phases 2 and 4)

| Host module | Into quantlab | Notes |
| --- | --- | --- |
| `src/research/stats.py` | `quantlab.stats.sharpe` | PSR and DSR. Pure functions. |
| `src/research/inference.py` | `quantlab.stats.inference` | Newey-West, block bootstrap, placebos, matched baselines. The ledger-reading part (`family_trials`, `log_trial`) is adapted: it reads the experiment store in Phase 5. |
| `src/research/strategy_checks.py` | `quantlab.validation.cards` | The strategy card. `card_sleeve`/`card_basket` stay host-side (they use the host portfolio backtest) until the engine replaces it. |
| `src/research/signal_checks.py` | `quantlab.validation.cards` | The signal card, with the same standard-file mechanism. |
| `research/strategy_standard.toml`, `research/signal_standard.toml` | lab standards folder | Versioned pass marks. Keep their history. |
| `src/research/governance.py` | `quantlab.governance` | Holdout, pre-registration locks, manifests. The ledger moves to the experiment store (Phase 5) with an importer for the existing `trial_ledger.jsonl`. |
| `src/research/benchmark.py` | `quantlab.analytics.benchmark` | Beta, alpha, capture. |

### Features, signals, regimes: port (Phase 3)

| Host module | Into quantlab | Notes |
| --- | --- | --- |
| `src/backtest/indicators.py` | `quantlab.features.indicators` | The causal rolling primitives. The host's strategies keep importing through the wrapper. |
| `src/signals/regime_features.py` | `quantlab.features.regime` | ER, slope t, ADX, variance ratio, walk-forward percentiles, ADF. |
| `src/signals/trend_forecast.py` | `quantlab.signals.trend` | Already follows the signal convention (unit variance, cap). |
| `src/research/volatility.py`, `src/risk/sizing.py` (estimators) | `quantlab.risk.volatility` | EWMA and HAR forecasts. The sizer classes are adapted: the host sleeves keep their sizer API. |
| `src/research/regime_switch.py` | split | The trend and range models become signals; the state machine becomes `quantlab.regimes.feature_blend`; the overlay and weights become `quantlab.overlays`; the H14 backtest loop is **retired** onto the engine. |
| `src/research/carry.py`, `seasonal.py`, `pairs.py`, `features.py`, `forecast.py` | `quantlab.signals.*` / `quantlab.features.*` | Ported when a study needs them. |
| `src/backtest/strategies.py` | **keep**, later wrap | The runtime registry's binary strategies. In Phase 7 each becomes a quantlab signal with a host wrapper for the registry. |

### Backtesting: retire onto one engine (Phase 2)

| Host module | Treatment | Notes |
| --- | --- | --- |
| `src/research/regime_switch.backtest`, the `h11`/`h13` study loops, `src/research/multi_asset.py` book code | retire | Each wrote its own decide/trade/earn and cost logic. The engine replaces them; the golden numbers prove equivalence. |
| `src/research/portfolio.py` (`simulate_portfolio`) | port, then generalise | The best existing multi-instrument simulator: weights at the close, fees, slippage, funding, bands. Becomes the core of the engine's portfolio simulation. |
| `src/research/bar_engine.py`, `src/research/execution.py`, `src/research/costs.py` | port | Next-open fills, actual funding, intrabar stops, maker fill modelling, volatility-scaled costs. They become the engine's execution and cost models. |
| `src/portfolio/backtest.py` | keep, later wrap | The research mirror of the paper engine, used by `card_sleeve` and `portfolio_backtest.py`. It stays until Phase 7, where paper and live run quantlab strategies and the parity monitor compares them. |
| `src/research/engine.py`, `src/backtest/simple_backtest.py`, `src/backtest/simulator.py`, `src/backtest/walk_forward.py`, `src/backtest/runner.py` | retire (research use); keep `runner.StrategyRegistry` | The single-symbol sweep engine and simple backtester. Superseded once the engine and the validation suite (walk-forward, CPCV) exist. `main.py --demo-backtest` and `--walk-forward` are re-pointed or dropped. |
| `src/portfolio/allocation.py`, `risk.py`, `risk_model.py`, `netting.py`, `exposure*.py` | port the math, keep the runtime wiring | Allocation methods, the covariance model, exposure limits and netting are pure and belong in the engine's portfolio layer. The paper engine keeps using them through wrappers. |

### Data: adapt (Phase 1)

| Host module | Treatment | Notes |
| --- | --- | --- |
| `src/research/multi_asset.py` (Yahoo, OECD, Norges Bank fetchers), `src/research/regime_switch.load_market` | port as adapters | Public sources with no host state: they become quantlab adapters (`quantlab.data.adapters.yahoo`, `.oecd`, `.norges_bank`). |
| `src/research/engine.load_bars`, `src/data/historical.py`, `src/data/kraken_futures.py` (history), `src/data/binance_archive.py` | adapt | Host caches and venue specifics. Adapters in `src/lab_adapters/` implement quantlab's data interface. |
| `src/research/pit.py` | port the method | The `available_at` point-in-time join is exactly the lab's convention and becomes part of the data layer. |
| `config/futures_markets.csv`, `research/data/multi_asset_markets.csv` | merge | Into the instrument master (Phase 1.1), with a converter so the futures runtime reads the master. |
| `src/futures/contracts.py`, `sessions.py` | port the calendar/roll math | Contract months, last trading days, rolls and liquid windows are needed for held-contract returns. The runtime keeps its board and adapter. |

### Reporting and notebooks

| Host item | Treatment | Notes |
| --- | --- | --- |
| `src/research/plots.py`, the study chart code in `scripts/research/h14_study.py` | port | Into `quantlab.reporting.charts` (matplotlib, no forced backend). |
| `src/portfolio/tearsheet.py` | keep | The running book's tearsheet. Later reads the lab's chart library. |
| `notebooks/strategy_card.ipynb`, `signal_card.ipynb`, `h14_regime_overlay.ipynb` | rewrite in Phase 6 | They become the lab's templates on the `Lab` API. The current versions keep working until then. |

### Keep in the host (the trading runtime)

`main.py`, `src/runtime`, `src/execution`, `src/portfolio/engine.py`, `book.py`, `runtime.py`, `orders.py`,
`feed.py`, `src/futures/adapter.py`, `feed.py`, `wiring.py`, `src/storage`, `src/risk/controls.py` and
`kill_switch.py`, `src/dashboard`, and the tax ledger. In Phase 7 the paper and live engine runs quantlab strategy
objects; until then it is untouched. It keeps its database (`data/cryptoquant.db`); the lab gets its own.

## Golden numbers: what must reproduce

`packages/quantlab/tests/golden/studies.json` lists them with their sources and tolerances. Before any module is
retired, the engine must reproduce its study's numbers within tolerance, or the difference must be explained in the
migration log below and accepted by the owner.

## Migration log

| Date | Step | Result |
| --- | --- | --- |
| 2026-10-19 | Phase 0: package, config loader, conventions, compliance note, this plan, golden numbers recorded | done |
