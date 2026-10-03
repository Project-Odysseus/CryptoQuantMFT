# Code optimization / revision register

This file tracks code that should be improved later without changing behavior right now.

| File | Area | Reason |
|---|---|---|
| `src/backtest/walk_forward.py` | `evaluate_walk_forward` (used by `main.py --walk-forward`) | Measures each fold on realized-only equity, so a trade opened in the training window credits its whole P&L to the test window, and every fold force-closes its position (an extra exit fee plus truncated trends). The research toolkit (`src/research/engine.py`) avoids both by splitting one continuous mark-to-market run; `--walk-forward` still uses the old method. Deferred 2026-09-25. |
| `src/portfolio/engine.py`, `src/portfolio/allocation.py` | `Allocator` at a cold start (`inverse_vol`, `risk_parity`, `hrp`) | The runtime steps the allocator over every history bar on its first cycle, so its refit schedule counts from the first candle it was given, while `run_book` counts from the config's `measure_start`; for the covariance methods those catch-up bars also use the sleeves' latest weights, not the ones held then. Engine and research targets then differ by up to 3-6% of equity on the synthetic test books (0 under `equal` and `fixed`, which the live and paper configs use). A fix needs a start date both sides share (e.g. refits on a calendar schedule, like the basket's rebalance days). Found 2026-10-03 while adding the risk model, deferred. |
