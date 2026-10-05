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

Everything below the first line is needed before live runs for days unattended, server or not. The long-term
direction is in `aspirations.md`.

- [ ] An always-on machine (section 3). For a local test, `caffeinate -i` stops the Mac from sleeping
- [ ] A paper soak of `config/portfolio.btc_live.toml` on real candles: at least a few days before live, 2-4 weeks
  before scaling up (portfolio plan phase 5). Watch memory, candle gaps, restarts and alert volume.
  **Started 2026-10-03** on the Mac as a LaunchAgent (`deploy/launchd/com.cryptoquant.paper.plist`, state in
  `data/portfolio/btc-live-paper/`, output in `logs/paper/`); tick this once it has run clean for the days above
- [x] A fault-injection soak test (2026-09-27, `tests/test_fault_injection_soak.py`): the real live adapter and engine
  for ~1,700 cycles against a Kraken Futures that times out, returns 503s and rate limits, rejects, loses responses
  and fills late. It found a real bug (a fill reaching `/fills` after its order was written off was never booked),
  now fixed
- [x] A crash and restart drill in paper (2026-10-03): `scripts/drills/restart_drill.py` (kill -9 at six points inside a
  decision and at random, on three configs) and `scripts/drills/network_drill.py` (the network cut around a kill -9, on
  real candles). The first run found a real fault, now fixed: a kill between the exchange's fill and the checkpoint left
  the book and the exchange apart for good (`todo_important.md`)
- [x] The same drill live at one lot (2026-10-03, `scripts/drills/live_restart_drill.py`): the live book was killed with
  SIGKILL right after Kraken accepted its first order (buy 0.0001 BTC at 84,619) and restarted. 17 of 18 checks passed:
  one order on Kraken, nothing sent twice, the book equal to the account, the 20% stop resting, the position closed at
  84,618, cost 0.0084 USD. The failed check found a bug, fixed (`todo_important.md`)
- [x] The drill's buy was missing from the trade log and the tax ledger (its fee, 0.0042 USD): added 2026-10-03 with
  `scripts/maintenance/backfill_drill_buy_20261003.py --write`
- [x] Paper and live runs of one config no longer share a name in `portfolio_snapshots` (2026-10-03): a live book is
  stored as `<name>-live`, and the dashboard, tearsheet and performance review show the two apart
- [x] A dead-man's switch and a daily summary (2026-09-27, `src/utils/heartbeat.py`; runbook). To switch it on:
  create a healthchecks.io check and set `HEALTHCHECK_URL` in `.env`
- [x] Retries with backoff on Kraken's temporary errors (timeouts, 429/5xx, `EService:Unavailable`, rate limits),
  separate from real rejections (2026-09-27, `src/utils/retry.py`). Reads and cancels only; orders are never resent
- [ ] A supervised first live session (a day or two, checked a few times a day): first-cycle adoption, orders at the
  bar close, fills, reconciliation, tax rows, Telegram
- [ ] Run it live (runbook, "Live portfolio trading"). In the first week, check the dashboard, drift alerts and the tax queue daily
- [x] Trigger the kill switch once against a real open position at minimum size (2026-09-26: `--kill-switch` now closes Kraken Futures positions and cancels orders on its own; verified live against a 0.0001 BTC long). In the first live week, trigger it once more while the book runs
- [x] Funding and fees from Kraken's account log (2026-09-28): real fees per fill and funding trued up on totals each decision bar,
  into the book and the tax ledger (`fee_true_up` / `funding_true_up`). Checked read-only against the real account's log
- [x] A catastrophe stop resting on Kraken for every open position (2026-09-28, `[risk] exchange_stop_pct`, 20% in the BTC book):
  reduce-only stop-market on the mark price, replaced before cancelled, fills booked even after downtime, sleeves kept out after.
  Tested against the fake Kraken only: watch the first live placement in Kraken's open orders
- [x] Kill criteria per sleeve, written before live (2026-09-28, `[review]` in `portfolio.btc_live.toml`, checked by
  `performance_review.py`): a Sharpe 2 standard errors below the backtest's after 90 days, negative after a year, or a book
  drawdown beyond 1.5x the backtest's worst
- [x] A cooldown after repeated order rejections (2026-09-28): 3 in a row on an instrument -> only reductions on it for 24 hours
- [x] The open items in `todo_important.md` (all done 2026-09-27)

## 2. Portfolio (plan: `docs/portfolio_plan.md`)

- [x] Phases 0-4: the sizing contract, research backtester, pure core (config, sleeves, allocation, netting, risk overlay, order planner, research/runtime parity), stateful book with paper execution and restarts, and the `--portfolio` runtime with candles, snapshots, dashboard and alerts (2026-09-26)
- [x] Phase 6 live path: a multi-contract Kraken Futures adapter, async fills, restart-safe client ids, a reconciliation block, adopt-exchange, funding, tax, the live gates and a money cap, plus the first real round trip (2026-09-26)
- [x] Sizing: the `[portfolio] scale` knob and `scripts/research/risk_budget.py` (2026-09-26)
- [x] Basket sleeves (plan 7.4, 2026-09-29): `[[baskets]]` in the portfolio config (`src/portfolio/basket.py`), the taker-buy book
  in `config/portfolio.taker_paper.toml` (40 Kraken perps from `scripts/portfolio/basket_instruments.py`), research/runtime parity
  tested through a restart. Minimum sizes checked: one lot of the priciest coin is ~15 USD, so it needs a few hundred USD per
  coin weight; paper only until the account is a few thousand USD
- [ ] Measure Kraken's real spreads and book depth on the basket's 40 coins (public order books, no keys) and replace the
  assumed slippage tiers: on Kraken prices the basket made nothing in-sample and fails at 2x costs, so costs decide it
  (research log 2026-09-29)
- [ ] Only if real costs are below the tiers: paper-run the taker basket (`--runtime paper --portfolio config/portfolio.taker_paper.toml
  --runtime-interval 300`) next to the BTC book on the always-on machine
- [x] Phase 7, correlation-aware risk (2026-10-03): a risk model of the instruments (`src/portfolio/risk_model.py`), caps on beta to
  BTC, on book volatility at crash correlations and per instrument group, an exposure summary in every snapshot and on the
  dashboard, alerts when the strategies become one bet, baskets as one unit under risk parity/HRP, a per-sleeve drawdown pause.
  All off by default. `config/portfolio.multi_paper.toml` (trend on BTC/ETH/SOL plus the taker basket) uses them: `equal` stays
  the allocation, the limits cut volatility and drawdown for 0.03 of Sharpe (research log 2026-10-03)
- [ ] Paper-run `config/portfolio.multi_paper.toml` on the always-on machine, once the basket's real costs are measured (above)
- [ ] Phase 7, left: regime filters as sleeve inputs. Sleeve health (disable after 3 failing cycles) and risk-parity/HRP
  allocation (2026-09-28) are done

## 2b. One engine for every strategy type (plan: `docs/engine_plan.md`)

- [ ] E0-E1: the strategy interface, intents and event loop, with the current sleeves wrapped and parity with `run_book`
- [ ] E2: execution policies with maker-first (maker-first itself is in the portfolio engine since 2026-10-04; the
  policy interface of the unified engine is not); E3: basket strategies (the cross-sectional book)
- [ ] E4-E6: options intents and exposure limits, intra-bar events, and retiring the single-strategy runtime

## 3. Run 24/7 (merges the old deployment, monitoring and security items)

- [x] Done: run-until-stopped mode for the portfolio, restart-safe checkpoints, the kill switch, alerts sent once per problem, and backup/restore steps in the runbook
- [x] A deployment kit in `deploy/` (2026-09-28): `deploy/setup.sh` for Ubuntu Server on the home desktop, systemd units with
  `Restart=always` (collectors, paper book), `.env` at `chmod 600`, log rotation, nightly backups (`deploy/README.md`)
- [ ] Install Ubuntu Server on the home desktop and run `deploy/setup.sh` (then stop the Mac's LaunchAgent)
- [x] A dead-man's switch and a daily Telegram summary (2026-09-27; set `HEALTHCHECK_URL`)
- [ ] Nightly backups of `data/cryptoquant.db` (SQLite `.backup`) and `data/portfolio/`, kept off the server (the tax ledger is in the database).
  The nightly job exists (`deploy/backup.sh`); still needs an off-machine `BACKUP_REMOTE`
- [ ] Security: allow the API keys only from the server's IP; confirm neither Kraken key can withdraw; document key rotation and incident response
- [ ] Single-strategy runtime, only if it keeps running unattended: bounded memory (`RuntimeOrchestrator.history`, and paper `run()` replaying every bar), bars from exchange candles, and HTTP off the event loop, as the portfolio feed does
- [ ] Recorders on the server (also options O1): the light liquidations + hourly option-chain collector runs as a LaunchAgent on
  the Mac since 2026-09-28 and moves with `deploy/setup.sh`. The full order-flow recorder (several GB a day) can go on the
  desktop's HDD; it's what the order-flow research in section 5 waits for

## 4. Tax (Norway)

- [x] Norges Bank FX with a cache, the ledger schema, realization-time events (spot FIFO; perp realized P&L, fees and funding), the EUR pool and currency gains, year-end holdings, and exports with a Skattemelding summary. Validated with real spot (2026-09-18/25) and futures (2026-09-26) trades
- [ ] January 2027: export the first real trading year, spot-check it against Kraken's history, and run the Dec 31 valuation checklist
- [ ] Confirm how Skatteetaten treats derivative flows (perp P&L and funding) before filing (ask during 2026; filing is spring 2027).
  Include the collateral conversions: Kraken pays USD fees and losses by converting EUR collateral (`conversion` entries in the
  account log), which are EUR disposals the ledger doesn't record yet
- Exact funding and fees from Kraken's account log: tracked in section 1

## 5. Research roadmap (findings: `docs/research_log.md`)

- [ ] Funding and OI as ranking signals in the cross-sectional work (carry timing was tested in H1: a regime trade, failed its holdout)
- [ ] Altcoin carry across the Binance archive (data downloaded 2026-09-26): basis from the premium index, and a cross-coin rule
- [ ] Order-flow features after ~4 weeks of recording (trade-flow and book imbalance, liquidation bursts, 1m-1h). Needs the full
  order-flow recorder running (section 3), not just the light collector
- [ ] OKX funding and OI in the recorder
- [ ] HAR volatility forecast in the runtime (needs 1h bars and a year of history at startup; EWMA is used now)
- [x] Research governance (2026-09-27, `src/research/governance.py`): a frozen final holdout from 2026-01-01, a global
  trial ledger for the deflated Sharpe, and run manifests. Also strategy clustering (13 strategies = 3.1 bets) and IC
  decay by horizon (only the funding z-score clears costs). Data plan: `docs/data_inventory.md`
- [ ] Paper vs backtest: after a few weeks of paper trading, replay `run_book` over the same bars and track the gap in
  fills, slippage and timing (the real cost model, and the first sign of decay)
- [ ] Binance `metrics` (OI, long/short ratios) for the Kraken-listed coins, to extend the crowding study beyond BTC/ETH
- [ ] Small free daily series: US spot ETF flows, stablecoin supply, the Coinbase premium, an event calendar (CPI, FOMC, expiries)
- [ ] A crowding-composite sleeve, sized against `total_trials()` (the funding z-score alone failed its placebo in H1, 2026-09-28)
- [x] H1b timed carry: one holdout look for "Binance fees, 14-day hold" (2026-09-28): failed (-0.10% Jan-Aug 2026, funding
  far below the hurdle). Revisit only if funding runs above ~10-15% a year again
- [ ] H2 v2 on real liquidations after 6-12 months of `scripts/collectors/run_collectors.sh`: first check that proxy
  events line up with liquidation notional > 5x median
- [ ] H3 phase B scoping (BTC short vol, defined risk): bid/ask economics on ~3 months of recorded hourly chains, or
  Tardis history. Needs the owner's approval
- [ ] CPI release dates into `research/data/` by hand (bls.gov blocks scripts), to complete H2's news split
- [x] Maker-first execution (2026-10-04): `[execution] policy = "maker_first"` rests each order at the touch as a post-only
  limit, books fills per cycle, and after `maker_timeout_seconds` cancels it and sends the rest to market. Paper
  (real Kraken bid/ask, pessimistic fill rule) and Kraken (`orderType=post`), restart drill passing. On for the
  multi-strategy paper book; every order logs a `portfolio_maker_result` event to measure fill rates.
- [ ] Maker-first on Kraken with real money: one live decision at one lot, check the resting order, its fill or its
  cancel-and-market fallback, before any live config uses it (needs the owner to run it)
- [ ] After two weeks of the paper book: read the maker results (share filled, seconds waited, drift paid on
  fallbacks) and the measured Kraken costs (`scripts/research/kraken_costs.py`), then set the basket's
  `slippage_bps` from the measurements instead of the assumed tiers
- [x] Small-account paper book (2026-10-05, `config/portfolio.small_paper.toml`): the five daily trend rules on BTC, ETH
  and SOL at 19 USD and scale 0.55, replacing the BTC-only paper book (state `data/portfolio/small-paper`). ETH and
  SOL lots are small enough to track their targets; BTC is still held in one-lot steps (45% of the book)
- [x] Signal lab (2026-10-04, `src/research/signal_lab.py`, `notebooks/signal_lab.ipynb`): five ideas side by side and combined
- [x] Strategies by market state and H7, a flat-market gate on the trend rules (2026-10-04): the gate failed on the
  unseen 2020-2022 sample, not built. `scripts/research/regime_study.py`, `research/reports/H7.md`
- [x] H9, a rank buffer on the taker basket (2026-10-05): killed (turnover -22%, return -5 points a year). `research/reports/H9.md`
- [x] H8, pairs of Kraken perps by the distance method (2026-10-04): killed (Sharpe -0.65, 12% of trades converged). `research/reports/H8.md`
- [x] H6, cross-sectional funding carry with a point-in-time Kraken coin list (2026-10-04): inconclusive (Sharpe 0.52,
  interval -0.72 to 1.82), not built. `research/reports/H6.md`
- [x] Paper books charge Kraken's real funding per coin, and the collector stores those rates (2026-10-04,
  `kraken_spreads.FundingRates`, `data/market_data/kraken_spreads/funding/`)
- [x] Web dashboard (2026-10-04, `src/dashboard/`): local read-only page per book (equity vs BTC, drawdown, exposure,
  limits, strategies, positions, correlations, fills, alerts, job health, measured costs). LaunchAgent on the Mac
- [x] Kraken spread and depth collector (2026-10-04, `src/data/kraken_spreads.py`) and `scripts/research/kraken_costs.py`:
  measured cost per coin against each config's assumed slippage
- [x] Paper against backtest (2026-10-04, `scripts/research/paper_vs_backtest.py`): positions per decision bar and return gap
- [x] Book tearsheet (2026-10-03, `src/portfolio/tearsheet.py`): one static HTML page with equity vs BTC, the underwater curve, a
  monthly heatmap, rolling vol/Sharpe/beta, historical VaR/ES, exposure over time and strategy correlations. Written by
  `portfolio_backtest.py` for a backtest and by `main.py --portfolio PATH --tearsheet` for a running book
- [x] Research tools for hypothesis work (2026-10-03; research guide, "From a hypothesis to a report"): a general event-study
  engine with matched controls and drawdown episodes (`src/research/events.py`), one standard signal report per idea
  (`src/research/signal_report.py`: data quality, frequency, event study, regimes, nearby definitions), and
  `scripts/research/new_hypothesis.py` (pre-registration template, study skeleton, a lock the study checks). No hypothesis has
  been run with them yet
- [ ] Parameter stability in the scorecard (from the QIS review, step 5): the best parameters per walk-forward window, with a purge
  gap between train and test and every configuration logged to the trial ledger. For event-type signals the signal report's
  "nearby definitions" table now covers this; strategies swept with `research.py sweep` still lack it
- [ ] Ideas from outside conversations (`chat_discussions.md`, 2026-10-03), each needing its own pre-registration before any test:
  monthly rotation between sleeves by recent performance (only worth testing with more than ~3 independent bets); an intraday
  signal used only to time the entry of a multi-day hold (the intraday studies failed at short holds, not at this); BTC/ETH
  drawdown episodes as the first use of the event engine (close to H2, which failed)
- [ ] Prediction markets (research only; `notebooks/prediction_markets.ipynb`, research log 2026-10-03). Before anything else:
  check the employer's personal-trading rules and whether Polymarket and Kalshi are open to Norwegian residents. Further
  questions, each to pre-register: model-versus-market gaps on weeks of settled Kalshi 15-minute markets; quoting instead of taking
- [ ] **H4 and H5, confirmatory run on or after 2026-11-17** (pre-registered and locked 2026-10-03; research log). Automated:
  the LaunchAgent `com.cryptoquant.research-daily` runs `scripts/research/h45_daily.py` once a day. It caches Kalshi's quotes
  for the chains recorded so far (no results shown) and, once the window is over and holds 20 settled daily events, runs both
  confirmatory studies once, saves them to `data/research/prediction_markets/H5_confirmatory.txt` / `H4_confirmatory.txt`
  and sends one Telegram message. Left for a person: judge them against the pre-registrations and write
  `research/reports/H4.md` and `H5.md`. The sample is only as large as the hours the option-chain collector was awake
- [ ] Binance 1-minute klines for BTC/ETH/SOL from the public archive (on wifi): they carry taker-buy volume, enough for a
  1-minute cumulative volume delta without paid tick data
- [x] A live performance review (2026-09-28, `scripts/research/performance_review.py`): the book vs buy-and-hold from the runtime's
  snapshots (beta, alpha, capture), fees, funding and sleeve P&L
- [ ] The rest of the review, after weeks of fills: slippage vs decision price, trade durations, alpha decay
- [x] Strategy design notes for options, options + perps and spot: `docs/options_plan.md`, section 8 (2026-09-26). Next from it: the tail-hedge vs lower-scale study and the spot-vs-perp venue rule for long holds

## 6. Options (plan: `docs/options_plan.md`)

- [x] Foundation and research framework (2026-09-26): pricing on the forward, the validation gate, the notebook's PDE model fixed and ported, calibration in vol points, the SVI surface, Deribit chain storage, exposures and scenarios, and `notebooks/options_research.ipynb`
- [x] O1: option chains recorded hourly since 2026-09-28 (the collectors; moves to the always-on machine with section 3)
- [x] Options building blocks (2026-09-27): USDC-settled chains, a contract picker with roll rules, exposure limits
  (`[risk.exposure]`), option positions in the book with expiry and tax, and a Deribit trading adapter (fake-exchange tests only)
- [x] O3b (2026-10-03): `[risk.exposure]` `max_delta` and `max_scenario_loss` are enforced on perp and spot targets; all four
  limits are checked on the held book (options included) and alerted; the dashboard has an exposure section. Option sleeves
  (E4) still need their own sizing against the vega and gamma limits (`scale_for_limits`)
- [ ] E4: option sleeves (tail hedge first) in the config and engine, marked from recorded USDC chains, paper first
- [x] O4: the IV vs realized-vol premium study (2026-09-28, H3: BTC +6 vol pts, t 3.4; ETH not significant; IV sizing no
  better than EWMA)
- [ ] O5: option instruments and sleeves in the portfolio (research first)
- [ ] O7: Deribit testnet paper trading. O8: live (needs Deribit keys, Norway eligibility, and more capital: the smallest BTC option is 0.1 BTC, the smallest linear USDC option 0.01 BTC)

## 6b. Options and prediction markets in the multi-strategy book (plan written 2026-10-04; nothing started)

**Goal:** one book that can hold perps, options and prediction-market contracts side by side, paper first, with one
risk overlay, one dashboard and one tax record. Sizes: S = a day or less, M = a few days, L = a week or more.

**Where we stand.** Options: pricing, surface, chains recorded hourly, option positions in the book with expiry and
tax, greek and scenario limits, a Deribit adapter tested on a fake exchange. No option strategy in the engine, no
paper exchange for options. Prediction markets: read-only Polymarket and Kalshi clients, a fair-value model, the
H4/H5 studies collecting until 2026-11-17. No order code, no instrument in the book, no paper mode.

**The one design problem both share.** Today a strategy outputs a *weight* per instrument, and the engine nets
weights and trades the difference. An option or a bet is a specific contract that expires, has a known worst loss and
is sized in contracts, not as a share of equity. Both tracks need the same new thing: a strategy that outputs
*positions in contracts* under a loss budget. Build that once (part A) and both tracks use it.

### P. Checks only the owner can do (none blocks paper; all block live)

- [ ] Employer rule: are crypto options allowed? Are prediction markets (crypto-linked ones, and others)?
- [ ] Access from Norway: Deribit (options), Kalshi, Polymarket. Each has its own country rules; read their terms
- [ ] "Freeport Markets": I don't know this venue. Its address and API documentation are needed before it can be
  planned (does it have a public order book API, what settles its markets, which countries it serves)
- [ ] Tax: how Norway treats prediction-market winnings (capital income or gambling) and option premiums; ask
  Skatteetaten or an accountant before any live bet
- [ ] Capital: the smallest Deribit USDC option is 0.01 BTC (about 850 USD of underlying). Prediction-market
  contracts cost under 1 USD each, so they fit a small account

### A. Shared foundation: contract strategies in the engine

- [x] A1 **Contract sleeves** (2026-10-04, `src/portfolio/contracts.py`): a strategy returns the contracts it wants
  (instrument, signed quantity, limit price, taker or maker); the engine keeps each sleeve's positions apart, cuts
  targets to its budget, and sends the difference through the normal write-ahead order path every cycle. Config:
  `[[contract_sleeves]]` (id, venue, strategy, budget, params). The strategy registry is empty until one passes its
  test. Paper only: live refuses a config with contract sleeves
- [x] A2 **Binary contracts in the book** (2026-10-04): kind `binary` next to perp, spot and option. Yes contracts at a
  price between 0 and 1; No is held as negative Yes units (`binary_stake` reports the cash the venue holds for it);
  `settle_resolved_binaries` pays 1 or 0; restart-safe; the engine settles through `binary_results` and books it
  for tax; snapshots list option and binary positions under `contracts`. Open: fee formulas live with the paper
  exchange (C3); a binary's delta in the exposure limits is A3
- [x] A3 **Sizing and limits for bets** (2026-10-04): a sleeve's bets are cut to its budget (money at risk), and
  `max_event_share` caps what one event may take of it (markets of one event win and lose together). A strategy
  gives each bet's coin and delta; the book's `[risk.exposure]` check then counts the bet as that many coins and
  alerts on a breach. Total risk is bounded by the budgets (at most half of equity). Open: the delta is *checked*,
  it does not yet reshape the perp targets; option sleeves against the vega and gamma limits come with part B
- [x] A4 **Limit orders, taker and maker** (2026-10-04): a taker bet fills what the book offers inside its limit and
  cancels the rest; a maker bet rests, is withdrawn after `maker_timeout_seconds` and is never sent to market (the
  strategy re-decides). Same `pending_orders`, settle and cancel path as perp orders
- [x] A5 **Allocation** (2026-10-04): contract sleeves get a fixed budget from the config, and their venue's paper
  account is funded with it. Open: including them in the risk model once they have a return history
- [x] A6 **Restart drill with bets** (2026-10-04): `restart_drill.py --bets` adds a sleeve that places a taker and a
  maker bet every bar and holds them to resolution; 28 of 28 on three seeds. It found two gaps, fixed: a bet bought
  in a cycle was valued at cost, not at the market, when the perp orders of that cycle were sized; and a fill's
  trade row and its event were two writes, so a kill between them logged the trade twice (now one transaction,
  for perps too) (a fill booked once, an expiry or
  resolution that happens while the process is down)
- [x] A7 **Dashboard** (2026-10-04): a "Bets and options" card per book: each contract sleeve's money at risk against
  its budget, and the contracts held (side, paid, now, value). Open: greeks, an expiry calendar, the tearsheet

### B. Options track (continues section 6: O5, E4, O7, O8)

- [ ] B1 (M) **Live chain quotes for the runtime.** The collector stores hourly chains; a running book needs the
  bid and ask of the contracts it holds or wants, each cycle (public Deribit data, a few requests)
- [ ] B2 (M) **Paper exchange for options.** Fills a limit order only against the real quote: a buy at or above the
  ask fills at the ask, otherwise it rests. Deribit's fee formula. Long options and defined-risk spreads only at
  first, so no margin model is needed
- [ ] B3 (M) **First option sleeve: a tail hedge.** A rolling put spread on BTC sized by budget, using the contract
  picker and roll rules that exist. Chosen first because it is long-only, simple, and useful to the trend book
- [ ] B4 (L) **Research before more sleeves** (each pre-registered): the tail hedge against simply running the book
  smaller; H3 phase B (selling BTC volatility with defined risk) once about 3 months of chains are recorded
- [ ] B5 (M) **Option backtest on recorded chains**, so paper can be compared with a backtest as the perp books are
- [ ] B6 (M) Deribit test network: the real adapter against test.deribit.com with test keys, restart drill (O7)
- [ ] B7 Live (O8): only after P, B6, and capital for at least the 0.01 BTC contract

### C. Prediction-market track

- [x] C1 **Order books are recorded** (2026-10-04, `src/data/prediction_recorder.py`): Kalshi and Polymarket BTC and ETH
  price markets, quotes and five levels of depth every 10 minutes, market descriptions and both venues' results;
  runs with the collectors. Open: a shorter interval on wifi
- [ ] C2 (M) **Market model.** Each market linked to what it pays on: underlying, strike or range, expiry, and its
  resolution source. Kalshi and Polymarket settle the "same" question on different price sources and averaging
  rules; that difference is a risk and must be stored per market, not assumed away
- [x] C3 **Paper exchange for binaries** (2026-10-04, `src/execution/binary_sandbox.py`): fills against the venue's live
  order book (walks the depth), fee `rate x p x (1-p)` (Kalshi rounded up to a cent), No positions with their
  stake set aside, resting orders, a state file, settlement from the recorder's result files (`RecordedResults`).
  Fees: Kalshi taker 0.07 and maker 0.0175; Polymarket taker 0.07, maker 0, nothing for selling a held token (the
  maker rates and the free selling are from a secondhand summary: check the venues' fee pages). Open: a backtest
  against the *recorded* books
- [ ] C4 (M) **First binary sleeves**, only for ideas that pass their test: what H4/H5 show on 2026-11-17 (options-
  implied probability against the quoted price; protection bought as "no" contracts); the same market on two venues
- [ ] C5 (M) **Hedged binaries.** A crypto-linked binary has a delta; the book's delta limit (A3) sees it, and a
  hedge sleeve trades the perp against it. This is the first strategy that truly mixes the two worlds
- [ ] C6 (L) **Trading access**, per venue, only after P: Kalshi (API key with request signing, USD account);
  Polymarket (a wallet, USDC on Polygon, signed orders: a different kind of custody and its own security review);
  Freeport (unknown until P). Each needs its adapter, a one-contract live drill like the Kraken one, and the gates
- [ ] C7 (S) Non-crypto markets, if ever: they have no delta and no model here; only the worst-case-loss limits
  apply, and each needs its own research. Not planned

### D. The combined book

- [ ] D1 (S) A paper config with trend sleeves, the basket, a tail hedge and one binary sleeve; cash per venue (the
  book already keeps cash per venue; moving money between venues stays manual)
- [ ] D2 (M) Kill switch and flatten across venues: perps close at market; options close at the bid or are left to
  expire by rule; binaries are sold at the bid or held to resolution by rule. The rule is set per sleeve
- [ ] D3 (S) Runbook, architecture map, and the paper-versus-backtest check for contract sleeves
- [ ] D4 13 weeks of the combined paper book before any live decision on the new parts

### Order, and where to start

1. **C1, the prediction-market order-book recorder (S).** It is the only item where waiting loses something: every
   day not recorded is data that can't be had later. It is small, read-only and independent of everything else.
2. **A1 and A2 together (M + M):** contract sleeves and binary contracts in the book, with tests. This is the
   foundation both tracks stand on.
3. **C2 and C3:** the binary paper exchange. Binaries come before options because they are the simplest contract
   (no surface, no greeks needed to hold one, worst loss known), they settle daily so paper gives answers in days,
   and they fit the capital. By then the 17 November result says whether there is a strategy to put in it.
4. **B1 to B3:** the options paper exchange and the tail hedge, reusing A1-A4.
5. **A3-A7, C5, D:** limits, drills, the hedged binary and the combined book.

Not before a strategy has passed its test: C4, B4's sleeves, and anything live. The engine work is worth doing only
if it ends with something to run in it; if H4 and H5 both fail and no option study passes, stop after step 2.

## 7. Other venues (API keys)

Kraken spot and Kraken Futures keys are configured (2026-09-26). Put keys in `.env` only, with no withdrawal
permission, and never print them.
- [ ] Binance, Bybit, OKX read-only keys: the real fee tiers, to replace the assumed fees in backtests
- [ ] Trading keys and an adapter per venue, only after a strategy has passed paper and dry-run on that venue
- [x] Deribit live key, read-only (account read, trade read, no wallet), checked with `--deribit-check` (2026-09-27)
- [ ] Deribit testnet keys (test.deribit.com, a separate account) for O7 paper trading; trade read+write only there

## 8. Housekeeping

- [x] `--preflight` (2026-09-28): every non-destructive check at once, with a pass/fail table (config, kill switch, database,
  clock vs Kraken, futures public and read-only, spot validate-only, Telegram, dead-man's switch; `src/runtime/preflight.py`)
- [ ] CLI smoke tests for `--demo-backtest` and `--walk-forward` (and fix `--walk-forward`'s fold accounting: `code-optimize.md`)
- [ ] Split `main.py` (~2,400 lines) into command modules, and `Decimal` wherever money is booked (`aspirations.md` 8)
- [ ] Standing rule: keep `README.md`, `docs/runbook.md` and `docs/architecture-map.md` in sync with runtime changes

## Deferred or not planned (kept for the record)

- **A funding-crowding filter on the trend rules** (2026-09-28): H1 found the contrarian sign reversed for OI-weighted funding
  z at 1-7 days (`research/reports/H1.md`). Not built.

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
- **2026-09-26, cleanup:** `.env.example` deleted (it held real credentials and nothing used it; it was never committed), the roadmap deduplicated, and `README.md` rewritten with a module map.
- **2026-09-26, live hardening:** paper shorts from flat, the spot `fee=0` reconciliation, the futures IOC write-off race, Kraken altname symbols, a trailing ATR stop, and tested runtime risk defaults (`todo_important.md`).
