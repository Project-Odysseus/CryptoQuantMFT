# Multi-asset plan: FX, equity futures and ETFs for a fund pilot

Written 2026-10-09. Nothing here is started. Sizes: S = a day or less, M = a few days, L = a week or more.

## Answers from the owner (2026-10-09) and where this stands

- **Instruments:** futures, and ETFs if an edge can be shown. Currencies are allowed. Bonds and commodities are
  covered by other strategies or not permitted, so the universe is equity index futures and currencies.
- **Credible means:** a good backtest, then a paper record, with Sharpe and Sortino ratios that are not negative.
- **Base currency NOK; budget 1,000,000 or 100,000 NOK.** No other restrictions now, but the code must allow adding
  them later (the risk overlay already takes limits per instrument, per group and for the book).
- **Handover:** a file of orders, fed to a system that sends it to the trading desk (phase 4, F1).
- **The fund today:** technical indicators on futures with mixed results and a discretionary overlay; trend followers
  on bonds; spread trading. The portfolio manager sits next to the owner at work. This is a proof of concept.
- **Not answered:** what data may be used. The first pass used free data.

Because the fund already runs technical indicators on futures, the first test was not trend alone but a book that is
half trend and half relative (carry and relative momentum), on equity indices and currencies. **Result (H11,
`research/reports/H11.md`): inconclusive.** Sharpe 0.32 after costs over 2000-2025, but 0.04 since 2013. Unrelated to
equities. In whole contracts the 100,000 NOK budget is too small for futures and 1,000,000 NOK loses half the
positions, unless the figure is a risk budget and not the capital. Phases 2 to 4 are not started, by the gate below.

## Goal

Show a colleague who runs a multi-asset fund a model credible enough to be given a small risk budget. That needs
three things, in this order:

1. **A result** on FX, equity index and (if the mandate allows) bond and commodity markets that survives costs and
   an honest out-of-sample test.
2. **A paper record** of that model run by the engine on real prices, which matches its backtest.
3. **A handover** the fund can actually use: daily target positions, a risk report in the fund's terms, and a
   record of how the model was tested.

The plan is built so that step 1 needs almost no engine work. If the result is not credible, we stop before
building steps 2 and 3.

## What a fund will judge it on

A fund manager giving out a risk budget will not look at the code. They will ask:

- **Does it add something the fund lacks?** Correlation with the fund's existing book matters more than the
  stand-alone return. A modest return that is uncorrelated with equities and bonds is worth more to them than a
  higher one that is not.
- **Is the test honest?** How many variants were tried, what was fixed in advance, what the unseen period showed.
  The pre-registration files, the trial ledger and the frozen holdout already answer this, and they carry over.
- **Are the costs real?** Commission per contract, half the spread, roll costs, and the fund's own execution.
- **How bad can it get?** Worst drawdown at the proposed volatility, longest flat stretch, behaviour in 2008, 2020
  and 2022.
- **Can it be reproduced?** One command that rebuilds the result from licensed data.
- **How much money can it take?** Capacity is not a concern for liquid futures at a small budget, but they will ask.
- **How is it operated?** Who sends orders, what happens when the model is down, who can stop it.

## Suggested first model

**Diversified trend following on futures, volatility-targeted.** Reasons:

- It is the best-documented systematic multi-asset strategy, with published evidence over many decades and a live
  industry track record. A fund manager already knows it, which makes it easy to judge.
- The trend sleeves the engine already runs on BTC, ETH and SOL are the same idea. The rules carry over unchanged.
- It tends to be uncorrelated with equities over long periods and did well in 2008 and 2022, which is what a
  multi-asset fund is short of.
- Daily bars are enough. No intraday data, no order-book data.

Two honest caveats to put in front of the colleague from the start:

- **Expect a modest Sharpe ratio.** Published long-run figures for diversified trend are roughly 0.5 to 0.8 after
  costs, and the industry's live results over the last fifteen years have been lower, with flat stretches lasting
  several years. These figures are from memory and must be checked against a source before they go in any document.
- **Breadth is where the return comes from.** With only FX and equity indices the result will be clearly weaker
  than with bonds and commodities included. Historically rates and commodities carried much of the return. Whether
  the mandate allows them is the first question to settle.

Later candidates, each its own pre-registered idea: FX carry, cross-asset carry (futures curve slope), and a
defensive overlay that sizes the fund's equity exposure by trend. None is planned until the first result is in.

## Futures or ETFs

| | Futures | ETFs |
|---|---|---|
| Shorting | Free and symmetric | Needs borrow, costs a fee, sometimes unavailable |
| Leverage | Built in, margin of a few percent | Needs financing |
| Costs | Low commission, tight spread, roll every quarter | Commission, spread, yearly fund fee, dividend tax |
| History | Long (decades) but needs paid data to be clean | Free and clean, but mostly from the 2000s |
| Smallest position | One contract can be large (an S&P 500 micro is about a tenth of an e-mini) | One share |
| Engine work | Expiry, rolls, multipliers | Dividends, borrow fees |

**Recommendation:** research and pilot in futures, because a long-short trend model needs cheap shorting and a fund
will have a futures account. Use ETFs only where no liquid future exists, and for a quick free first look at the
data. FX is traded as currency futures rather than spot or forwards, so it needs no separate instrument type.

## What the engine has and what is missing

Carries over unchanged: weight sleeves, allocation, netting, risk overlay, order planner, crash safety, restart
drills, the dashboard, the research governance, the tearsheet, the paper-versus-backtest check.

Missing, with where it shows up in the code:

| Gap | Today | Needed |
|---|---|---|
| Instrument kinds | `perp` and `spot` in config; options and bets added by contract sleeves (`src/portfolio/config.py`) | `future` (multiplier, tick size, currency, expiry, roll rule) and `etf` (dividends, borrow fee) |
| Trading calendar | Every market trades every day; a year has 365 days (`book_risk.py`, `review.py`, `tearsheet.py`, `risk_budget.py`) | A calendar per exchange; 252-day years for these markets; one book mixing both |
| Closing times | One daily close at 00:00 UTC | Tokyo, Europe and the US close at different hours. Decide once a day after the last close, trade at the next open or close |
| Missing bars | A market whose last bar is older than the newest is treated as stale (`engine.py` `run_cycle`) | A holiday is not a fault; a real data gap still is |
| Rolls | Perps never expire | Signals on a back-adjusted continuous series, positions in a specific contract, roll orders on a schedule, roll cost booked |
| Currencies | The book marks in a base currency with FX rates passed in; every instrument so far is in USD | Profit and margin in each contract's currency, converted daily; base currency set by the fund |
| Costs | Fee in % of notional plus slippage in bps, funding on perps | Commission per contract, half-spread in ticks, roll cost; for ETFs the yearly fee, dividend tax and borrow |
| Cash return | Cash earns nothing | Collateral earns the short rate; report return in excess of cash, as funds do |
| Data | Kraken and Binance loaders | A daily multi-asset loader with licensing the fund accepts |
| Paper exchange | Kraken-shaped (`cross_margin.py`) | A futures-broker-shaped one with sessions, contract months and margin per contract |
| Risk report | Volatility, drawdown, exposure, correlation inside the book | The same plus value-at-risk, beta to equities and bonds, and correlation with the fund's book |

`config/contracts.yaml` and `docs/FUTURES_GUIDE.md` are empty scaffolding from an earlier attempt. Phase 2 either
fills them or removes them.

## Data

Daily bars are small (a few megabytes for fifty markets over thirty years), so this is fine on a mobile hotspot.

- **Free, good enough for a first look:** ETF prices adjusted for dividends, central-bank FX rates (Norges Bank,
  the ECB, FRED), short rates from FRED. Free continuous futures series exist but are unreliable around rolls.
- **Paid, needed for anything shown to the fund:** clean individual-contract futures history with roll dates.
  Options are a retail end-of-day vendor, a pay-per-use exchange data vendor, or a broker's history feed. Prices and
  terms need checking; none is chosen.
- **Best case:** the fund has a licensed terminal or data feed and can export history. That also settles the
  licensing question, since free sources generally do not permit commercial use.

Rule: the first look at any of this data happens after the idea is pre-registered. All of it is unseen today,
which is an advantage we lose the moment we plot it.

## Phases

### Phase 0. Answers and checks (owner; blocks the pilot, not the research)

- [ ] The questions at the end of this file
- [ ] The fund's mandate: which instruments, which markets, leverage and shorting limits
- [ ] Compliance on both sides: whether the owner may supply a model to the fund, in what role, and who owns it
- [ ] Whether running an algorithm brings the fund under algorithmic-trading rules (testing, kill switch, records).
      This is for the fund's compliance officer to answer, not something this plan can decide.
- [ ] Data licence: what the fund has, or a budget for a vendor

### Phase 1. The research result (start here; no engine changes)

- [ ] M1 (S) Market list and contract table: multiplier, tick, currency, exchange, roll rule, typical spread and
      commission, for about 20 to 50 liquid futures depending on the mandate
- [ ] M2 (M) Daily loader into the research layer, with an exchange calendar and a continuous series built two
      ways (back-adjusted for signals, actual contracts for profit and loss). Tests on roll days.
- [ ] M3 (S) Cost model: commission, half-spread, roll cost, return in excess of cash
- [ ] M4 (S) Pre-register H11: the trend rules already in use, the market list, volatility target, cost
      assumptions, pass and kill criteria, the holdout. Locked before any chart is drawn.
- [ ] M5 (M) Run it: full history, by sector, by decade, the frozen 2026 window once with approval. Bootstrap
      interval on the Sharpe ratio. Correlation with equities, bonds and a 60/40 mix.
- [ ] M6 (S) Report `research/reports/H11.md` and a tearsheet.

**Gate:** if the lower end of the interval is not above zero after costs, or the result depends on one sector or
one decade, stop and say so. Do not build phases 2 to 4 on an inconclusive result.

### Phase 2. Engine support (M to L in total)

- [ ] E1 (M) Instrument kind `future` in config and book: multiplier, currency, expiry, profit and loss per contract
- [ ] E2 (M) Exchange calendars: per-instrument sessions, holidays that are not faults, a year length per
      instrument for risk numbers
- [ ] E3 (M) Rolls: the roll schedule as planned orders through the normal order planner, crash-safe like any other
      plan, roll cost in the attribution
- [ ] E4 (S) Currencies: per-currency cash and margin, daily conversion, FX exposure of margin shown in risk
- [ ] E5 (S) Cost fields per contract, and interest on cash
- [ ] E6 (S) Instrument kind `etf` only if phase 0 says ETFs are needed
- [ ] E7 (S) Backtest-engine equality: the portfolio backtest and the engine give the same numbers on the same data,
      as they do for crypto today

### Phase 3. Paper trading (M)

- [ ] P1 (M) Paper futures broker: fills at the next session's price plus half the spread, margin per contract,
      contract months, expiry
- [ ] P2 (S) Daily price feed with a fixed decision time after the last market closes
- [ ] P3 (S) Restart drill extended to a roll day and a holiday
- [ ] P4 (S) Dashboard: contracts held, days to roll, exposure by sector and currency
- [ ] P5 A paper book on a LaunchAgent, read against its backtest after 4 and 12 weeks

### Phase 4. The fund pack (M)

- [ ] F1 (S) Daily target file: contracts per market, change since yesterday, the reason in one line. The fund's
      own desk trades it. This is the recommended handover: no order ever leaves this code, which keeps the fund's
      controls intact and the regulatory surface small.
- [ ] F2 (S) Reconciliation the other way: read the fund's fills and positions, compare with the targets, report
      the difference and the cost actually paid
- [ ] F3 (M) Risk report in the fund's terms: volatility against budget, value-at-risk, beta to equities and bonds,
      correlation with the fund's book, stress dates
- [ ] F4 (S) Method document: what the model does, how it was tested, what was tried and dropped, known weaknesses
- [ ] F5 (S) Stop rules agreed in advance: the drawdown or behaviour at which the pilot is cut, and who decides

### Phase 5. The pilot

- [ ] Small risk budget, daily targets, weekly reconciliation
- [ ] Review after three and six months against the backtest's expected range, using the same check as paper
- [ ] Scale up, hold or stop on the rules from F5

## Order and where to start

Start with **M1 and M4**: the market list and the pre-registration. Both are cheap, neither touches the engine, and
both need the answers to questions 1 to 4 below. M2 follows as soon as the data source is known.

The research (phase 1) can run on ETF and free data first to shake out the code, as long as the pre-registered
result is run on proper futures data.

## What this does not change

- The crypto books keep running as they are. Nothing in phases 1 to 4 touches the live gates.
- The owner's personal trading stays crypto only. Everything here is research and paper until the fund trades it
  in its own account.
- Options and prediction markets (TODO section 6b) are independent of this and can wait.

## Questions

**Needed before phase 1 can start**

1. **Which instruments does the mandate allow?** Futures, ETFs or both. And which sectors: only FX and equity
   indices, or bonds and commodities too? This decides the market list and how strong the result can be.
2. **What does the colleague mean by credible?** A backtest with a clean out-of-sample test, a paper record of some
   length, a minimum Sharpe ratio, low correlation with their book? If they have a checklist, we build to it.
3. **What data can we use?** Does the fund have a terminal or a licensed feed that can export daily futures
   history, or is there a budget for a vendor?
4. **What is the base currency and how is the risk budget defined?** For example NOK, EUR or USD; and a budget
   stated as volatility, value-at-risk, a maximum loss, or a notional amount. Roughly how large?

**Needed before phases 3 to 5**

5. **Who would send the orders?** The fund's desk from a daily target file (recommended), or this code through the
   fund's broker? If the second, which broker.
6. **Is the colleague at your employer, and is the fund your employer's?** This decides who must approve it, and
   who owns the model.
7. **How often may it trade?** Daily decisions with trades once a day is the assumption. Would weekly be preferred?
8. **Is there anything the fund already runs that is similar?** A trend or managed-futures allocation would make
   this redundant, and a different model would be the better pitch.
9. **Should crypto be part of what is shown?** Probably not for a regulated fund, but listed bitcoin futures could
   be one market among fifty if the mandate allows.
10. **What is your role meant to be?** Supplying signals, advising, or managing. This changes the compliance answer.
