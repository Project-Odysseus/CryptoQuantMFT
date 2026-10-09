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

## Build plan, second version (2026-10-09)

The owner's direction after H11: use micro futures, build the system out properly (real contracts and rolls, trading
hours around the world, liquidity as an input), horizon days to weeks, then look at other strategies. This replaces
the earlier phase list. Nothing below is started.

### New project or continue here: continue here, in separate modules

What was checked: the portfolio engine is about 6,400 lines in 22 files. Kraken is named in 8 of them and funding
in 6, and almost all of that sits in three files (`engine.py`, `feed.py`, `backtest.py`). Allocation, netting, the
risk overlay, the order planner, the risk model, exposure limits and the tearsheet do not mention a venue at all.

| | Continue here | New project |
|---|---|---|
| Reused as is | Sleeves, allocation, netting, risk overlay, order planner, crash safety and restart drills, research governance, dashboard, 900+ tests | Nothing; each is rewritten or copied |
| New work | Contracts and rolls, calendars and sessions, liquidity, order file, paper futures broker | The same, plus everything in the row above |
| Risk | The new parts get tangled with crypto assumptions (365-day years, 00:00 UTC closes, funding) | Months before the first paper trade; the crash-safety work is the slowest part to redo |
| Handing it to the fund | Needs the futures parts to be separable | Clean from the start |

**Recommendation: continue here, with one rule.** Everything new goes in modules that import nothing crypto-specific
(a `src/futures/` package for contracts, calendars, rolls and liquidity; the order file and the paper broker as
adapters). The engine reaches them through the seams it already has: the instrument table, the feed and the adapter.
If the fund later wants its own repository without the crypto books and the Norwegian tax ledger, those modules and
the engine core lift out; the old single-strategy runtime (`main.py`, `src/runtime`) stays behind.

The one reason to start a new project now would be ownership: if the employer will own this, it should not live
beside the owner's personal trading code. That is question 2 below, and it decides this more than any technical point.

### What is different about futures, and the design for each

**1. A future is a series of contracts, not one instrument.**
- A *market* (for example `cme:MES`) is what strategies, risk and limits see. A *contract* (`cme:MESZ6`) is what is
  held and traded. The book holds contracts; exposure per market is the sum over its contracts.
- A contract table gives each market its multiplier, tick size, currency, contract months, last trading day and
  exchange. It is data, not code, so adding a market is adding a row.
- Signals run on a continuous series built from the individual contracts (back-adjusted, so a roll is not a price
  jump). Profit and loss always uses the real contract's prices.
- Rolling: a market switches to the next contract when the next one trades more than the current one, with a
  fallback of a fixed number of days before the last trading day. The roll is two planned orders (close one, open the
  other) through the normal order planner, so it is crash-safe like any other plan. Its cost is booked and shown.
- H11 had none of this: it used index closes and a flat cost per roll.

**2. Markets are open at different hours, and liquid for only part of them.**
- Most index and currency futures trade nearly around the clock, but the order book is thin outside the home
  market's day. So "is it open" is the wrong question; "is it liquid now" is the right one.
- Each market gets an exchange calendar (holidays, half days) and a *liquid window* in local time, for example the
  hours its cash market is open.
- The daily bar of a market ends at that exchange's settlement time, not at 00:00 UTC. Every bar carries the time it
  became known, and a signal may only use bars known at decision time.
- The engine gets several decision slots a day instead of one, for example one each for Asia, Europe and the
  Americas. At each slot it recomputes targets from everything known and trades only the markets whose liquid window
  is open; the rest wait for their own slot. A holiday is not a fault; a missing bar on a trading day is.
- Risk is checked on the whole book at every slot, using the latest known price for markets that are closed.

**3. Liquidity is an input to sizing and to costs.**
- Static: a cost per market in ticks (half the spread plus commission), from the contract table.
- Measured, when a quote feed exists: spread and depth by hour of day, recorded the way the Kraken collector does now.
- In sizing: an order may not exceed a set share of the contract's average daily volume, and a market whose front
  contract trades below a floor is skipped. A micro contract can be thin even when its full-size sibling is deep, so
  volume is read per contract.
- In the backtest: the same cost and participation rules, so the backtest and the engine agree.

**4. Money.** Margin per contract instead of leverage as a multiple of equity; profit in the contract's currency
converted to NOK daily; interest on cash; years of 252 trading days per market instead of 365.

**5. Output.** No order leaves this code. Each slot writes an order file (market, contract, side, quantity, order
type, limit, valid-until, reason) for the desk's system, and reads a fills file back. Until fills arrive the book
treats the orders as pending, as it does today for resting orders.

**6. Restrictions later.** The risk overlay already takes limits per instrument, per group and for the book. Added
here: a maximum number of contracts per market, a list of markets that may not be traded, and allowed slots per
market. All in config, all optional.

### Micro contracts

Using micros is right, and it narrows the list. From memory and to be verified against each exchange before use:
micro S&P 500, Nasdaq 100, Russell 2000 and Dow on CME; micro DAX, Euro Stoxx 50 and SMI on Eurex; micro Nikkei on
Osaka; mini Hang Seng in Hong Kong; micro EUR, GBP, AUD, JPY, CAD and CHF against the dollar on CME. No micro for
the FTSE 100, CAC 40, NOK, SEK or NZD that I know of.

Two consequences:
- **History.** Most micros are recent (the CME equity micros date from 2019). Research uses the full-size contract's
  price history, which is the same price, and the micro's own volume for the liquidity rules.
- **Size is still a problem.** A micro equity index contract is roughly 290,000 to 350,000 NOK and a micro currency
  contract 70,000 to 150,000 NOK. H11 in whole micro contracts on 1,000,000 NOK of capital lost half its positions.
  This works only if the budget is the risk the fund accepts, not the capital. Question 1 below.

### Phases

Ordered so that research can use each piece as soon as it exists. The paper broker and the order file come last,
because they are only worth building for a strategy that has passed.

**Phase A. Data and contracts (blocks everything; M once the data source is known)**
- [ ] A1 (owner) Choose the data source: daily open, high, low, settlement, volume and open interest per individual
      contract, for the full-size and micro contracts. Question 3 below.
- [ ] A2 (S) Contract table: market, exchange, multiplier, tick, currency, months, last trading day rule, liquid
      window, cost in ticks. Every row checked against the exchange's own specification.
- [ ] A3 (M) Loader for individual contracts into a local store, with the time each bar became known
- [ ] A4 (S) Exchange calendars: holidays and half days per exchange (a maintained library, pinned)

**Phase B. Research layer for futures (M)**
- [ ] B1 (M) Continuous series with volume-based rolls; tests on roll days; roll dates saved so the engine uses the
      same ones
- [ ] B2 (S) Liquidity model: cost in ticks, participation cap, volume floor
- [ ] B3 (M) Session-aware backtest: decision slots, liquid windows, bars known at decision time, whole contracts,
      roll costs, margin, NOK conversion. Checked against a hand-worked example.
- [ ] B4 (S) Rerun H11 unchanged on this data as a check of the free-data result (a new version of its
      pre-registration, counted as trials)
- [ ] B5 New strategy ideas, each pre-registered. Candidates are listed under "Strategies to look at" below.

**Gate:** phases C and D start when one idea passes its pre-registered test on phase B. Phase C1 to C3 may start
earlier if the owner wants the engine ready in advance; they are useful to any futures strategy.

**Phase C. Engine (L)**
- [ ] C1 (M) Markets and contracts in config and book: instrument kind `future`, profit per contract, margin
- [ ] C2 (M) Calendars and decision slots in the runtime; holiday handling; per-market year length in risk numbers
- [ ] C3 (M) Rolls as planned orders; roll cost in the attribution
- [ ] C4 (S) Currencies and interest on cash
- [ ] C5 (S) Liquidity rules in the order planner; the new optional limits
- [ ] C6 (S) The portfolio backtest and the engine give the same numbers on the same data, as they do for crypto

**Phase D. Paper and handover (M to L)**
- [ ] D1 (M) Paper futures broker: fills inside the liquid window at the recorded price plus cost in ticks, margin,
      contract expiry
- [ ] D2 (S) Order file writer and fills file reader, in the desk system's format
- [ ] D3 (S) Restart drill on a roll day, a holiday and across two slots
- [ ] D4 (S) Dashboard: contracts held, days to roll, exposure by region and currency, next slot
- [ ] D5 (M) Risk report and method document for the fund
- [ ] D6 A paper book, read against its backtest after 4 and 12 weeks

**Phase E. The pilot.** As before: small risk budget, weekly reconciliation, stop rules agreed in advance.

### Strategies to look at (after phase B; none tested, each needs its own pre-registration)

- The currency half of H11 alone, at a slower pace. It was the stronger half; the 1.4% a year in costs came mostly
  from the fast trend rule and weekly resizing.
- Calendar effects in equity index futures: the days around month-end, and the days of central-bank decisions.
- The overnight and daytime parts of the index return, which behave differently. This is where knowing each
  market's session matters, and it fits a horizon of days.
- Lead and lag between regions: whether a large move in one region's session carries into the next region's open.
- Volatility-managed exposure: holding less when volatility is high.

These are less crowded than the textbook strategies of H11, and I have not checked whether any holds up.

### Decisions (owner, 2026-10-09) and what follows from them

- **Size: 10,000,000 NOK.** At that size whole micro contracts work: a micro equity index contract is 130,000 to
  345,000 NOK and a micro currency contract 80,000 to 150,000 NOK, so a typical position is several contracts. The
  H11 book in whole contracts loses 9% of its positions at 10,000,000 NOK against 49% at 1,000,000.
- **Ownership: the owner's, in this repository.** The futures parts stay in separate modules so they can be split
  off if that changes.
- **Data: only what is free, at daily frequency.** Three consequences:
  - Expired contracts are not available for free; only the contracts trading now are. So history cannot be built
    from real contracts. The backtest history is the index or spot rate plus interest rates (as in H11), and the
    engine works with real contracts from today on. Real roll data builds up from when recording starts; each
    contract's history disappears from the free source when it expires, so recording should begin before December.
  - The free source carries the CME contracts but not Eurex. The three European micros are in the table, but they
    cannot be paper traded on real quotes until there is another source. Without them the list is four US indices
    (which move together), the Nikkei and six currencies.
  - Volume in the free source looks right for the micros and wrong for some full-size currency contracts, so the
    liquidity rules use the micros' own volume.
- **Order file: CSV**, with an Excel copy if the desk wants one.
- **Hours: Oslo, with automatic sending possible later in the day.** Two decision times inside Oslo office hours
  cover every market in the table: about 10:00 Oslo for the European contracts, and about 16:00 Oslo (half an hour
  after the US open) for the US indices, the Nikkei contract and the currencies, which all trade on CME. No Asian
  session is needed. The slots stay configurable.

Still open: which exchanges and broker the fund uses (decides whether the European micros are in), and the exact
columns the desk's system wants.

### Progress

- [x] A2 (2026-10-09) Contract table `config/futures_markets.csv`: 14 micro contracts with multiplier, tick,
      currency, settlement, months, last trading day, liquid window, the series used for history and the free quote
      symbol. Sizes and ticks checked against CME and Eurex material; the status column says what is not confirmed
      (the Canadian dollar contract, how the yen contract settles, Eurex last trading times). Liquid windows and
      costs in ticks are assumptions until measured.

- [x] First part of the futures package, `src/futures/` (2026-10-09), tested in `tests/test_futures_contracts.py`:
      `contracts.py` (the table, contract codes, last trading days, roll day, volume-based earlier roll),
      `sessions.py` (exchange holidays, liquid windows, release of orders at the first slot inside the window),
      `liquidity.py` (participation cap and volume floor), `quotes.py` (daily bars of live contracts, a store, latest
      price), `order_file.py` (the CSV for the desk's system and the fills file back). The desk uploads the file to
      SimCorp Dimension, so the exchange and broker do not matter here. `exchange_calendars` added to `environment.yml`.
- [ ] Not built yet: the daily recorder script and its background job; the engine side (instrument kind `future`,
      a feed of daily bars, the session-bound adapter that queues orders until a slot and writes the order file or
      fills on paper, rolls booked in the book, NOK as base currency with zero foreign cash at the start, futures
      kept out of the tax ledger); a paper book config; the backtest in whole contracts. Design notes: the engine
      already accepts orders that fill later (`SUBMITTED`, then `settle_orders`), which is the seam for the adapter.

### Next

B1 to B3 on free data: the history series per market from index or spot plus interest rates, the liquidity rules,
and the session-aware backtest in whole contracts and NOK. Then A3 as a daily recorder of the live contracts, so
real rolls accumulate. Then new strategy ideas.

## What this does not change

- The crypto books keep running as they are. Nothing in phases 1 to 4 touches the live gates.
- The owner's personal trading stays crypto only. Everything here is research and paper until the fund trades it
  in its own account.
- Options and prediction markets (TODO section 6b) are independent of this and can wait.
