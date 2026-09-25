# Priority 3 execution tracker

This file breaks **Priority 3: Close the Live-Execution Gap Safely** into concrete subtasks. Each item should be checked off here when completed, and mirrored back into `TODO.MD`.

- [x] **Subtask 1: Define the live-dry-run execution path**
  - [x] Trace the current `paper`, `live_dry_run`, and `live` runtime wiring from CLI to orchestrator to adapters
  - [x] Identify the exact missing seams that block `live_dry_run` from acting as the pre-live promotion lane
  - [x] Write the target behavior for `live_dry_run` vs `live` so the implementation keeps trading disabled by default
  - **Current wiring**
    - CLI accepts `--runtime paper|live_dry_run|live` and passes it into `RuntimeConfig`, `ExecutionRouter`, `PaperTradingEngine`, and `RuntimeOrchestrator`.
    - `paper` runs with no execution adapter.
    - `live_dry_run` already gets a sandbox adapter shaped like the requested exchange.
    - `live` currently selects exchange adapters, but `RuntimeOrchestrator.run_once()` still aborts with `NotImplementedError` before execution.
  - **Blocking seams**
    - The runtime still treats `live` as a hard stop instead of a guarded execution path.
    - `live_dry_run` is not yet explicitly documented/enforced as the only pre-live promotion lane.
    - There is no explicit operator approval gate or second confirmation path for real `live`.
    - There is no acceptance coverage proving promotion/failure behavior across `paper`, `live_dry_run`, and guarded `live`.
  - **Target behavior**
    - `paper`: baseline source-of-truth lane using paper fills and the existing reporting/debug stack.
    - `live_dry_run`: use exchange-specific routing/reconciliation surfaces and production-like runtime state handling, but never permit production order placement.
    - `live`: remain blocked unless explicit safety gates, operator confirmation, kill-switch readiness, and strict caps are satisfied.
    - The next implementation order should be: `live_dry_run` orchestration, Kraken state hardening, `live` safety gate, operator checklist, then acceptance tests.

- [x] **Subtask 2: Wire `live_dry_run` through the runtime orchestrator**
  - [x] Route `live_dry_run` cycles through exchange adapters and reconciliation without enabling production trading
  - [x] Preserve paper-mode behavior as the source-of-truth baseline
  - [x] Persist health, order, reconciliation, and kill-switch context for `live_dry_run`
  - **Completed changes**
    - Resolved a real exchange-selection seam so `live_dry_run` now aligns its sandbox execution adapter with the effective market-data venue instead of drifting to generic `sandbox`/`USD` state when exchange selection is `auto`.
    - Aligned Firi fallback behavior so missing Firi credentials move both the market-data connector and the dry-run adapter back to Kraken together.
    - Kept `paper` behavior unchanged while adding explicit tests that `live_dry_run` runs through exchange-shaped adapter/account-state handling and still avoids real live execution.

- [x] **Subtask 3: Harden Kraken adapter state handling**
  - [x] Verify symbol mapping, remote order IDs, balances, and reconciliation payload handling against Kraken-shaped responses
  - [x] Ensure non-destructive auth/status/cancel flows behave correctly when credentials are missing or remote responses are partial
  - [x] Add tests for order lifecycle and account-state recovery using Kraken-shaped fixtures
  - **Completed changes**
    - Kraken order submission/status handling now normalizes exchange-shaped order payloads instead of depending on one narrow `QueryOrders` shape.
    - Kraken recovery now accepts Kraken-style balance and order payloads and converts them into the generic reconciliation structure used by the runtime.
    - Kraken adapter state now defaults to `EUR` cash handling, while cancel/status flows preserve local staged state when credentials are absent.

- [x] **Subtask 4: Add safety gates for live promotion**
  - [x] Prevent accidental `--runtime live` execution during development
  - [x] Add a second confirmation / explicit enablement mechanism for real live mode
  - [x] Require kill-switch readiness and strict low-risk caps before any live promotion path is considered valid
  - **Completed changes**
    - Added CLI-level live guards requiring `--enable-live-trading` plus the exact `--live-confirmation ENABLE_LIVE_TRADING` token.
    - Blocked `--runtime live` when exchange selection is still `auto`, when a mock connector is requested, when the kill switch is active, or when exchange caps exceed the conservative live ceiling.
    - Added a kill-switch readiness helper plus tests covering both rejected and allowed guarded-live requests.

- [x] **Subtask 5: Add operator approval and runbook checks**
  - [x] Create a manual approval checklist for `paper -> live_dry_run`
  - [x] Create a stricter manual approval checklist for `live_dry_run -> live`
  - [x] Document the non-destructive verification steps operators must complete before promotion
  - **Completed changes**
    - Added explicit runbook checklists for `paper -> live_dry_run` and `live_dry_run -> live`.
    - Added a concrete exchange-shaped `live_dry_run` command for Kraken as the near-term promotion lane.
    - Documented the operator checks required before any guarded `live` attempt and before any later promotion with real capital.

- [x] **Subtask 6: Add acceptance coverage for promotion and failure handling**
  - [x] Add tests for `paper -> live_dry_run` promotion behavior
  - [x] Add tests for stale data, order rejection, reconciliation mismatch, and cancel failure paths
  - [x] Confirm the runtime still rejects real live trading unless all explicit live guards are satisfied
  - **Completed changes**
    - Added promotion coverage showing `paper` keeps no execution adapter while `live_dry_run` adds exchange-shaped sandbox routing.
    - Added acceptance coverage for dry-run rejection context, kill-switch cancel failure capture, and the guarded-live rejection boundary.
    - Kept explicit coverage that guarded `live` stays behind the CLI safety gates and conservative exchange caps.

- [x] **Subtask 7: Implement the guarded `live` runtime path**
  - [x] Replace the current `NotImplementedError` hard-stop in `RuntimeOrchestrator` with a guarded live execution flow
  - [x] Reuse the existing exchange adapter, reconciliation, health-report, and kill-switch surfaces without bypassing the new live CLI safety gates
  - [x] Keep real live execution conservative and explicit so the runtime behavior matches the documented promotion model
  - **Completed changes**
    - Replaced the old live-mode hard stop with an incremental exchange-backed cycle path instead of replaying the full paper history every runtime loop.
    - Kept `live_dry_run` and `live` on the same exchange-backed execution model so staged orders can remain `SUBMITTED/OPEN` across cycles without being auto-canceled as fake paper rejections.
    - Added coverage proving guarded `live` can run through the orchestrator path while still staying behind the CLI safety gates and without requiring this session to place real orders.

- [ ] **Remaining Priority 3 closeout checks**
  - [x] Add a non-destructive Kraken verification command covering auth, balances, open/closed orders, status/cancel probes, and validate-only order requests
  - [x] Add a kill-switch preview against recovered Kraken order state
  - [x] Add preview-only manual Kraken open and close-position flows
  - [x] Add guarded manual Kraken open and close-position submission flows
  - [x] Add a concise account-summary/holdings surface for balances, positions, open orders, and recent live/manual actions (2026-09-25: `--account-summary` in `main.py`, `_run_account_summary`). Non-destructive: fetches live Kraken balances/positions/open orders (if credentials configured), pair metadata + current ask to compute "smallest order Kraken will accept right now" in EUR, plus recent trades and recent manual/live action events. Verified live against the real account: EUR 14.1367, flat, no open orders, current BTC/EUR minimum ≈3.69 EUR (binding on `ordermin`, not `costmin`). Along the way, fixed `TradeLogger.list_events` to support filtering by `event_types` via SQL — without it, the last-500-events window was entirely crowded out by `runtime_cycle_completed`/`runtime_health_snapshot` noise and the actual manual-action events (days old) never surfaced.
  - [x] Execute one tiny real Kraken round-trip (2026-09-18: manual BTC/EUR buy 5.021e-05 @ 69712.4, close/sell @ 69967.5, ~0.0128 EUR realized gain) — trades persisted via `cli_manual_live_order`/`cli_manual_live_close`
    - **Found and fixed a real gap:** both tax-ledger writes silently failed at trade time (`EUR fiat pool is insufficient` / `asset lot inventory is insufficient for BTC`) because the EUR fiat pool was never seeded before the trade. Worse, the CLI printed `"Tax logging: recorded for this fill"` even though it had failed, since `log_trade` swallowed the exception and always returned a trade id.
    - Recovered the real EUR deposit from Kraken's own `Ledgers` endpoint (read-only, non-destructive): 15.00 EUR gross, 0.82 EUR Kraken deposit fee, 14.18 EUR net, at 2026-09-18T11:59:40Z.
    - Backfilled the tax ledger directly via `TradeLogger.log_fiat_conversion` (seed lot, historical timestamp) + `TradeLogger.record_trade_tax_events` for both trades, using the real Norges Bank EUR/NOK rate already cached for that date (10.8095). `tax_ledger`, `tax_eur_fiat_lots`, and `tax_asset_lots` now correctly reflect this trade. DB was backed up first (`data/cryptoquant.db.bak.20260925130620`).
    - Fixed the root cause: `TradeLogger.log_trade` now returns `(trade_id, tax_event_error)` instead of swallowing the failure, and the manual submit/close CLI paths (`_run_kraken_order_submission`, `_run_kraken_close_submission` in `main.py`) now print an explicit `"Tax logging: FAILED"` warning with the reason instead of falsely claiming success. Chosen over a hard preflight block: a posteriori backfill via Kraken's own ledger proved workable, so visibility at trade time (not blocking) was the right fix.
    - Still open: comparing recovered exchange history (order IDs, closed-order payloads) end to end against this trade has not been done yet — see the unchecked items below.
  - [ ] Validate partial-fill and resting-open-order behavior against real Kraken state instead of immediate-fill assumptions
  - [x] Confirm strategy-driven live orders obey the same Kraken minimum-size, precision, and balance checks as manual orders (2026-09-25: `KrakenExecutionAdapter.submit_order` — the method every strategy-driven live order routes through via `PaperTradingEngine.run_exchange_cycle()` → `_route_exchange_order()` — previously only checked `size > 0` and passed the strategy's raw size straight to `AddOrder`. It now fetches real Kraken pair metadata (cached per pair to avoid hammering the public API every cycle), rounds to `lot_decimals`, and rejects before calling `AddOrder` if the rounded size is below `ordermin`, the notional is below `costmin`, or the live EUR/base-asset balance is insufficient — the exact checks `submit_quote_order`/`submit_close_position` already used for manual orders. If the metadata/balance fetch itself fails (network/API issue), the check is skipped rather than blocking, matching this adapter's existing fail-gracefully behavior — Kraken's own `AddOrder` response remains the backstop in that case. Tests added in `tests/test_execution_adapters.py` (5 new: below-min-size, below-min-cost, insufficient quote balance, insufficient base position on sell, precision rounding). Not yet exercised against a real Kraken account with live strategy orders — only unit-tested.
  - [ ] Re-run Kraken verification when the account contains at least one real historical or open order
  - [ ] Confirm recovered real Kraken order IDs, symbol mapping, and reconciliation state from populated exchange data
  - [ ] Confirm kill-switch preview targets the expected real open Kraken orders

- [ ] **Position-level risk controls: closeout for the exchange-backed path**
  - [x] Added `RiskManager.evaluate_exit()` (time-stop + ATR stop-loss) and a rolling daily-loss circuit breaker (`RiskControlConfig.daily_loss_limit_pct`, distinct from the existing all-time peak `hard_stop_drawdown_pct`) on 2026-09-25. Fully wired into `PaperTradingEngine.run()` (the deterministic backtest/paper-mode loop) with tests (`tests/test_risk_controls.py`, `tests/test_paper_trading.py`).
  - [x] `run_exchange_cycle()` (the `live_dry_run`/`live` per-cycle path) got the daily-loss circuit breaker (day-start equity tracked as an instance attribute on `PaperTradingEngine`, so it survives across cycles within one running process but resets on restart — no checkpoint persistence yet).
  - [x] `run_exchange_cycle()` now gets the time-stop / ATR stop-loss exits too (2026-09-25). Root-caused first: `_current_account_state()` was faking `avg_entry_price` as just the *current* price, which would have made the ATR stop a no-op even if wired in blindly. Fixed at the source: `ExecutionAdapter._apply_fill_to_account_state()` (the shared base class fill handler used by Sandbox/Kraken/Firi) now tracks a real weighted-average entry price and open timestamp per position, cleared when the position returns to flat. `get_account_snapshot()` exposes both; `_current_account_state()` reads the real entry price instead of faking it, and returns the open timestamp too. `run_exchange_cycle()` computes `bars_held` by counting bars at/after that timestamp and calls the same `RiskManager.evaluate_exit()` used by `run()`, so a stale/adverse position now force-closes in `live_dry_run`/`live` exactly like it does in paper mode. Tests added in `tests/test_execution_adapters.py` (entry-price/open-time tracking across adds and a full close) and `tests/test_paper_trading.py` (exchange-cycle force-close on time-stop with no exit signal). Smoke-tested via `--runtime live_dry_run --use-mock-connector`.
  - [ ] The ATR stop implemented is a **fixed** stop distance from the entry price, not a trailing stop that ratchets in the position's favor (`TODO.MD`'s Phase 4 line still says "ATR-driven trailing stop-loss logic" — trailing behavior is a separate, not-yet-done enhancement).
  - [ ] Counterparty/balance-distribution monitoring (Firi NOK vs Kraken EUR/BTC) from the same `TODO.MD` section is still untouched.
  - [ ] `daily_loss_limit_pct`/`time_stop_bars`/`atr_stop_multiplier` defaults set in `main.py::build_runtime_orchestrator` (5% daily loss, 60-bar time stop, 3x ATR) are first-pass placeholder values, not tuned against any real backtest — revisit before relying on them for anything beyond paper mode.
