# Roadmap

A Python framework for systematic crypto trading on Kraken (spot and Kraken Futures perpetuals), from research to
paper to live. It has a multi-strategy portfolio, Norwegian tax records, and an options research track.

Where the detail lives:
- `todo_important.md`: live-execution hardening (Kraken spot and futures), with the incident history.
- `docs/portfolio_plan.md` and `docs/options_plan.md`: the build plans.
- `docs/research_log.md`: every study and its findings.

Tick items here, and in those files, when they are done.

## Status (2026-09-26): what is safe to run

| What | State |
| --- | --- |
| Research, backtests, notebooks | Safe any time (public data) |
| Paper portfolio (`--runtime paper --portfolio ...`) | Safe; runs until Ctrl-C with checkpoints |
| Kraken Futures live | The plumbing is proven (`--futures-live-test`: a 0.0001 BTC round trip on PF_XBTUSD, 2026-09-26). `config/portfolio.btc_live.toml` is ready behind the live gates but hasn't run live yet |
| Kraken spot live (single-strategy runtime) | Proven with tiny manual and strategy-driven round trips (2026-09-18/25). Open hardening in `todo_important.md` |
| Options | Research only; no execution |

## 1. Critical path: the BTC book live

- [ ] An always-on machine (section 3)
- [ ] A paper soak of `config/portfolio.btc_live.toml` for 2-4 weeks (portfolio plan phase 5)
- [ ] Run it live (runbook, "Live portfolio trading"). In the first week, check the dashboard, drift alerts and the tax queue daily
- [ ] Trigger the kill switch once against a real open position at minimum size (`--kill-switch` while the live book holds a lot)
- [ ] Funding and fees from Kraken's account log. They are estimated from the public hourly rates and the taker rate for now; the equity-drift alert catches gaps
- [ ] The open items in `todo_important.md`

## 2. Portfolio (plan: `docs/portfolio_plan.md`)

- [x] Phases 0-4: the sizing contract, research backtester, pure core (config, sleeves, allocation, netting, risk overlay, order planner, research/runtime parity), stateful book with paper execution and restarts, and the `--portfolio` runtime with candles, snapshots, dashboard and alerts (2026-09-26)
- [x] Phase 6 live path: a multi-contract Kraken Futures adapter, async fills, restart-safe client ids, a reconciliation block, adopt-exchange, funding, tax, the live gates and a money cap, plus the first real round trip (2026-09-26)
- [x] Sizing: the `[portfolio] scale` knob and `scripts/research/risk_budget.py` (2026-09-26)
- [ ] Phase 5: the paper soak (see section 1)
- [ ] Basket sleeves (plan 7.4), so the taker-buy cross-sectional book can run: Binance public daily klines for the signal and Kraken perps for orders. Check Kraken's minimum sizes for a 30-50 coin basket at our capital
- [ ] A cooldown after repeated order rejections. Rejections alert, but trading doesn't pause automatically
- [ ] Phase 7 enhancements: portfolio vol targeting, correlation-aware caps, regime filters as sleeve inputs. Sleeve health (disable after 3 failing cycles) is already done

## 3. Run 24/7 (merges the old deployment, monitoring and security items)

- [x] Done: run-until-stopped mode for the portfolio, restart-safe checkpoints, the kill switch, alerts sent once per problem, and backup/restore steps in the runbook
- [ ] A deployment kit in `deploy/`: a conda setup (or Dockerfile), a systemd unit with `Restart=always`, `.env` at `chmod 600`, and log rotation
- [ ] A dead-man's switch: ping an external monitor (e.g. healthchecks.io) every cycle. Also a daily Telegram summary (still alive, P&L, positions) at midnight UTC
- [ ] Nightly backups of `data/cryptoquant.db` (SQLite `.backup`) and `data/portfolio/`, kept off the server (the tax ledger is in the database)
- [ ] Security: allow the API keys only from the server's IP; confirm neither Kraken key can withdraw; document key rotation and incident response
- [ ] Single-strategy runtime, only if it keeps running unattended: bounded memory (`RuntimeOrchestrator.history`, and paper `run()` replaying every bar), bars from exchange candles, and HTTP off the event loop, as the portfolio feed does
- [ ] Recorders on the server: option chains first (small); the order-flow recorder needs disk or compression first (several GB a day)

## 4. Tax (Norway)

- [x] Norges Bank FX with a cache, the ledger schema, realization-time events (spot FIFO; perp realized P&L, fees and funding), the EUR pool and currency gains, year-end holdings, and exports with a Skattemelding summary. Validated with real spot (2026-09-18/25) and futures (2026-09-26) trades
- [ ] January 2027: export the first real trading year, spot-check it against Kraken's history, and run the Dec 31 valuation checklist
- [ ] Confirm how Skatteetaten treats derivative flows (perp P&L and funding) before filing
- [ ] Exact funding from Kraken's account log (section 1)

## 5. Research roadmap (findings: `docs/research_log.md`)

- [ ] Crowding (funding, OI, long/short ratios) as a filter on the daily trend rules
- [ ] Funding and OI as ranking signals in the cross-sectional work, and as carry timing
- [ ] Altcoin carry across the Binance archive (data downloaded 2026-09-26): basis from the premium index, and a cross-coin rule
- [ ] Order-flow features after ~4 weeks of recording (trade-flow and book imbalance, liquidation bursts, 1m-1h). Needs the recorder running (section 3)
- [ ] OKX funding and OI in the recorder
- [ ] HAR volatility forecast in the runtime (needs 1h bars and a year of history at startup; EWMA is used now)
- [ ] Calendar and event features (CPI, FOMC, option expiries, CME weekend gaps)
- [ ] A never-touched final holdout for strategy selection (the deflated Sharpe is done)
- [ ] Maker-first execution (post-only limits, then taker after a timeout): the lever most likely to make intraday edges tradable
- [ ] A live performance review script: equity vs BTC buy-and-hold, trade durations, slippage vs expected price, alpha decay, cost-adjusted P&L
- [ ] Strategy design notes for options, options + perps and spot: `docs/options_plan.md`, section 8

## 6. Options (plan: `docs/options_plan.md`)

- [x] Foundation and research framework (2026-09-26): pricing on the forward, the validation gate, the notebook's PDE model fixed and ported, calibration in vol points, the SVI surface, Deribit chain storage, exposures and scenarios, and `notebooks/options_research.ipynb`
- [ ] O1: start `--record-option-chains` on the always-on machine
- [ ] O3b: exposure limits (`[risk.exposure]`) in the risk overlay, and an exposure section on the dashboard
- [ ] O4: the IV vs realized-vol premium study
- [ ] O5: option instruments and sleeves in the portfolio (research first)
- [ ] O7: Deribit testnet paper trading. O8: live (needs Deribit keys, Norway eligibility, and more capital: the smallest BTC option is 0.1 BTC, the smallest linear USDC option 0.01 BTC)

## 7. Other venues (API keys)

Kraken spot and Kraken Futures keys are configured (2026-09-26). Put keys in `.env` only, with no withdrawal
permission, and never print them.
- [ ] Binance, Bybit, OKX read-only keys: the real fee tiers, to replace the assumed fees in backtests
- [ ] Trading keys and an adapter per venue, only after a strategy has passed paper and dry-run on that venue
- [ ] Deribit: options trading, and testnet keys for O7

## 8. Housekeeping

- [ ] A `--preflight` command that runs every non-destructive check at once (Kraken spot validate-only, futures read-only, the Telegram test, `--portfolio-check`)
- [ ] CLI smoke tests for `--demo-backtest` and `--walk-forward`
- [ ] Standing rule: keep `README.md`, `docs/runbook.md` and `docs/architecture-map.md` in sync with runtime changes

## Deferred or not planned (kept for the record)

- **Dated-futures scaffolding** (`src/execution/contracts/`, `src/execution/sizing/`, `config/contracts.yaml`, `docs/FUTURES_GUIDE.md`, three unit tests): superseded by perpetuals and the portfolio. The placeholder integration test was removed on 2026-09-26.
- **Firi (NOK) trading and Kraken/Firi counterparty monitoring**: all trading is on Kraken.
- **WebSocket market data**: REST candles are enough for 4h and daily decisions (portfolio plan 4.1). Revisit for intraday strategies or private fill streams.
- **Intraday price/volume strategies**: the first study found nothing tradable at our fees (research log, 2026-09-26). Revisit with order-flow data or maker execution.
- **Single-exchange z-score/OU mean reversion, an order-book signal suite, an ML signal aggregator (xgboost/SHAP)**: mean reversion and short-horizon momentum lost net of costs in every study. Revisit only with better data.
- **A home Windows server**: replaced by the always-on plan in section 3.
- **A more realistic L2 simulator** (queue position, adverse selection): only needed for order-book strategies.

## Done (milestones; details in `todo_important.md`, `docs/` and git history)

- **Foundations:** `environment.yml` (Python 3.13), logging, the data pipeline and storage, an L2 simulator, the fee model, the paper engine and order state machine, the SQLite trade logger with reports and dashboards, the kill switch, runtime orchestration with a watchdog, and the live gates with approval checklists.
- **2026-09-18/25, Kraken spot live:** the guarded live runtime, manual preview/submit/close, the first real round trips, a tax backfill, and reconciliation fixes.
- **2026-09-25, research and risk:** the research toolkit (strategies, sweeps, in-sample/holdout, heatmaps), the sizing contract, risk exits (time, ATR, drawdown, daily loss), shorts end to end, aligned exit semantics, and `strategy_id` attribution.
- **2026-09-25/26, perpetuals:** the contract and margin model, the sandbox, verified Kraken specs, the single-contract live adapter, and perp tax flows.
- **2026-09-26, research data and studies:** perp history since 2020, positioning data, the Binance archive (859 coins), and the order-flow recorder. Studies: intraday, positioning, volatility, carry, cross-sectional, diversification, portfolio, and risk budget. Also the deflated Sharpe, the signal notebook and the scorecard.
- **2026-09-26, portfolio and options:** portfolio phases 0-4 and the live path with the first real Kraken Futures round trip, Telegram trade messages with reasons, and the options research framework.
- **2026-09-26, cleanup:** `.env.example` deleted (it held real credentials and nothing used it; it was never committed), and the roadmap deduplicated.
