# quantlab

A portable research-to-live lab for systematic multi-asset strategies: one engine from idea to live capital, causal
by construction, statistics-first.

- **Plan and checklist:** `docs/research_lab_plan.md` in the host repository.
- **Conventions every component follows:** `CONVENTIONS.md`.
- **Ownership and trading restrictions:** `COMPLIANCE.md`.

## Install

```bash
pip install -e packages/quantlab              # from the host repository's root
pip install -e "packages/quantlab[test]"      # with pytest
pytest packages/quantlab/tests
```

Core dependencies: numpy, pandas, scipy, matplotlib (tested versions in `requirements.lock`).

## Status

Phase 0 (foundations):
- the package boundary;
- the strict config loader (`quantlab.config`);
- the conventions as code (`quantlab.conventions`).

Phase 1 (data):
- `quantlab.calendars`;
- `quantlab.data`: instruments, adapters, the versioned catalog, data cards, futures construction, panels, universes
  and macro series.

```python
from quantlab.data import InstrumentMaster, DataCatalog, load_panel
master = InstrumentMaster.load("config/instruments.toml")
panel = load_panel(master, ["idx:NDX", "idx:SPX"], catalog=DataCatalog("data/quantlab"))
panel["close"]; panel.cards["idx:NDX"].to_markdown(); panel.snapshots
```

Phase 2 (engine):
- `quantlab.engine`: components, `run_backtest`, returns and costs from the master, the cache;
- `quantlab.portfolio`: allocation, construction, contracts;
- `quantlab.analytics.performance`;
- `quantlab.strategies.regime_overlay`: H14's strategy as components.

```python
from quantlab.engine.backtest import run_backtest, Sizer
from quantlab.engine.components import signal, apply_signal, Targets
from quantlab.analytics.performance import metrics
mom = signal("mom_60", lambda bars: np.sign(bars["close"].pct_change(60)), warmup=60)
result = run_backtest(Targets(apply_signal(mom, panel)), returns, costs_bps=1.5, sizer=Sizer(0.10))
metrics(result)
```

Phase 3 (regimes as research objects):
- `quantlab.signals.library`: the signal catalog;
- `quantlab.regimes`: models, HMM, simulator, bench;
- `quantlab.validation.regime_card`;
- `quantlab.overlays.overlay`;
- `quantlab.analytics.conditional`.

Walkthrough: `notebooks/regime_lab.ipynb`.

Phase 4 (validation), in `quantlab.validation`:
- `strategy_card` / `signal_card` / `regime_card`, with pass marks in `validation/standards/*.toml`;
- `power`, `crossval`, `robustness`, `scenarios`;
- `ledger`: trials, cards, pre-registrations, holdout looks; `$QUANTLAB_LEDGER`;
- `fdr`: lab-wide false-discovery control.

Walkthrough: `notebooks/validation_lab.ipynb`.

Phase 5 (record and reporting):
- `quantlab.tracking.store`: runs and the hypothesis registry, in `quantlab_data/research/lab.db`;
- `quantlab.reporting.report.build_report`: one HTML report per strategy;
- `quantlab.reporting.charts`: the charts library.

Walkthrough: `notebooks/lab_record.ipynb`.

Phase 6 (the notebook interface):

```python
from quantlab.lab import Lab
lab = Lab(universe="equity_index_core", start="1999")
idea = lab.signal(lambda px: (px.close.rolling(20).mean() / px.close.rolling(100).mean() - 1).rank(axis=1, pct=True) - 0.5)
lab.test(idea)
lab.combine(idea, "trend", overlay="h14_regime")
lab.compare_with_book(idea)
```

Fill the catalog once, offline, from this project's caches: `python scripts/lab/import_research_cache.py`.
Templates: `notebooks/templates/`.
