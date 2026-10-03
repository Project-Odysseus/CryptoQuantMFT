# Code optimization / revision register

This file tracks code that should be improved later without changing behavior right now.

| File | Area | Reason |
|---|---|---|
| `src/backtest/walk_forward.py` | `evaluate_walk_forward` (used by `main.py --walk-forward`) | Measures each fold on realized-only equity, so a trade opened in the training window credits its whole P&L to the test window, and every fold force-closes its position (an extra exit fee plus truncated trends). The research toolkit (`src/research/engine.py`) avoids both by splitting one continuous mark-to-market run; `--walk-forward` still uses the old method. Deferred 2026-09-25. |
