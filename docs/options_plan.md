# Options in the portfolio: exposures first, pricing models you can test

How options fit into the portfolio (`docs/portfolio_plan.md`): a strategy is designed by the exposures it takes, and
a new pricing model can be dropped in and tested before anything relies on it. Status 2026-09-26: design, plus the
pricing and validation foundation (`src/options/`).

---

## 1. What we want

- **Design a strategy by its exposures.** Say "short BTC vega, delta-neutral, at most 1% of equity per vol point",
  or "cap the loss at 20% in a -30% crash". Don't pick contracts by hand. The book shows its delta, gamma, vega and
  theta per underlying at all times, and what it would lose in a set of shock scenarios.
- **One book.** Options, perps and spot net against each other by underlying: an option sleeve's BTC delta and the
  trend sleeves' BTC perp position are one BTC exposure. A hedging sleeve can target the total.
- **Pricing models are plug-ins with a test gate.** Every model implements one interface. It must pass the
  validation harness before it is used for risk, and a trading test on recorded market data before it is used for
  signals.

## 2. Principles

1. **Price on the forward.** Deribit quotes each expiry against its own forward (`underlying_price`). Use Black-76:
   the forward, a discount factor, and no drift. Treating the forward as spot and adding an interest rate moves the
   forward twice (section 6 measures what that costs).
2. **Market implied vol for risk, models for opinions.** Greeks and scenario P&L use a surface fitted to market
   prices, so risk agrees with how the market marks the book. A model's view (e.g. "these wings are cheap") is a
   trading signal, and it has to earn its place like any other signal.
3. **Exposure limits, not notional limits.** An option's notional says little about its risk. The risk overlay
   limits delta, vega, gamma and scenario losses.
4. **Honest costs.** Trade at bid and ask, never at mark. Deribit's option fee is 0.03% of the underlying, capped at
   12.5% of the option's price, plus spread and slippage. Model-vs-mark gaps smaller than this are not edges.
5. **Data before strategies.** Deribit's historical option chains aren't free. Start recording chain snapshots now
   (it's small: about 1,500 BTC and ETH contracts, a few MB a day), so every later test has real bids, asks and IVs.

## 3. Architecture

```
Deribit public API ──> chain snapshots (recorded) ──> per expiry: forward + fitted IV smile (SVI)
                                                              │
                        src/options/pricing.py  <─────────────┤  models: Black76, MertonJump (done),
                        src/options/validation.py (done)      │  SVI surface, Heston, local-vol-jump PDE
                                                              ▼
 sleeves (trend, carry, option structures) ──> targets as EXPOSURES per underlying
        ──> allocation ──> netting by underlying (perp delta + option delta)
        ──> exposure risk overlay (delta / vega / gamma limits, scenario loss cap)
        ──> order planner (options: pick contracts, limit orders near mid, roll rules)
        ──> adapters: Kraken Futures (perps), Deribit (options; testnet for paper)
        ──> book: positions, greeks, P&L attribution (delta / gamma / vega / theta / residual)
```

**Instruments.** The config gains `kind = "option"` on a venue (`deribit`), with an underlying and settlement
(`inverse`, BTC-settled, or `linear`, USDC-settled). Sleeves don't name contracts: they state a structure, and
the planner picks the listed contracts that match it.

**Exposure layer (`src/portfolio/exposure.py`, next to build).** Every position maps to exposures per underlying:
- **delta** in money (units x price for perps and spot; model delta x contracts x forward for options);
- **gamma** as the change in money delta for a 1% move;
- **vega** in money per vol point, bucketed by expiry;
- **theta** in money per day.

It also gives a **scenario grid** of P&L under spot moves of ±10/20/30/50% crossed with IV shifts of ±10/20
points. Perps and spot have delta only, so the current book gets its first exposure report as soon as this lands
(BTC and ETH delta in money).

**Risk overlay extension.** `[risk.exposure]` limits per underlying: `max_delta` (share of equity), `max_vega`
(share of equity per vol point), `max_short_gamma`, and `max_scenario_loss` (the worst grid cell as a share of
equity). When a limit binds, it scales option sleeves down, as the current caps scale weights.

**Option sleeves.** Each outputs a structure and a size in exposure terms, for example:
- `covered_call`: sell a 25-delta call about 30 days out against a share of the BTC trend position; roll 5 days
  before expiry.
- `tail_hedge`: buy 10-delta puts about 60 days out, sized so that the -30% scenario loss is at most X% of equity.
- `vol_carry`: sell a delta-hedged strangle when IV minus forecast realized vol (the HAR/EWMA forecasts in
  `src/research/volatility.py`) is above a threshold, sized by vega.
- `model_signal`: trade the gap between a validated model and the market, only after section 4's trading test.

The sleeve's delta joins netting, so a delta-hedged strangle's hedge can come from the perp book instead of
separate trades.

## 4. How a pricing model is tested (the gate)

1. **Validation harness** (`validate_model`, done): bounds, monotone and convex in strike, put-call parity, delta
   bounds, calendar order, and equality with Black-76 when the model's extra features are switched off. Run it over
   a strike x expiry grid. A model that fails isn't used.
2. **Convergence** (grid and Monte Carlo models): halve the grid spacing and the time step; the price must settle.
3. **Calibration quality:** the IV error in vol points (not the percentage price error, which weights cheap wings
   far too heavily), by moneyness and expiry. Compare with the fitted SVI smile as a baseline.
4. **Stability:** calibrated parameters shouldn't jump from day to day. If they do, the model is fitting noise.
5. **Out of sample:** calibrate on day t, then price day t+1's chain at its new forward. A model that only fits
   the day it was fitted to explains nothing.
6. **Trading test:** on recorded chains, trade the model's gaps at bid and ask with fees, delta-hedged, and
   attribute the P&L (delta, gamma, vega, theta, residual). Apply the deflated Sharpe (`src/research/stats.py`) over
   every variant tried.

## 5. Build plan

| Step | What | Needs |
| --- | --- | --- |
| O1 | Record Deribit chain snapshots (BTC and ETH, hourly: forward, bid, ask, mark IV, OI per contract) | nothing (public API, small) |
| O2 | Surface: a forward and an SVI smile per expiry, with arbitrage checks; greeks from market IV | O1 |
| O3 | Exposure layer and scenario grid for the current book; dashboard section; `[risk.exposure]` limits | nothing |
| O4 | Vol research: the IV vs realized-vol premium (DVOL history is already cached), by tenor and regime | nothing |
| O5 | Option sleeves in the research backtester, first on a synthetic DVOL-based surface, then on recorded chains | O1-O3 |
| O6 | Port the notebook's PDE model as a `PricingModel` (with the fixes in section 6) and put it through the gate | O1 |
| O7 | Paper-trade on Deribit's testnet (test.deribit.com: same API, free) | Deribit testnet keys |
| O8 | Live, behind the same gates as perps | Deribit account and keys |

## 6. Review of the options notebook (`/Users/Sakarias/option trading/main.ipynb`)

The models are a good start: the Merton series, a local-vol-plus-jumps PDE, a forward PIDE that prices the whole
surface in one sweep, and a vega-weighted calibration. The analytical Merton is now in `src/options/pricing.py`
(`MertonJump`, restated on the forward) and passes the validation harness. Running the notebook's code through the
same checks found these issues, all reproducible:

1. **The forward is treated as spot, then drifted at 4.5%.** Deribit's `underlying_price` is already the forward.
   With jumps and skew off, the notebook's PDE priced calls 3.5% (K=70k), 6.3% (100k) and 9.7% (130k) above the
   exact Black-76 value. Setting spot to forward x discount matches it within 0.01%. This alone produces false
   "Sell (Overpriced)" signals that grow with the strike.
2. **One forward for every expiry.** `S0 = results[0]["underlying_price"]` takes the forward of whichever contract
   Deribit lists first, not the Dec-26 expiry being priced. Use each option's own `underlying_price`.
3. **Deep in-the-money call deltas above 1** (1.07 at K=20k, 1.05 at 40k, as the notebook's output also shows). The
   local-vol skew is anchored at ln(S0), so the grid derivative holds the anchor still while a real move shifts it.
   With the skew off the grid delta is exactly 1.000. Re-pricing the same model with S0 moved ±0.1% gives 0.992. Use
   bump-and-reprice greeks (`src/options/pricing.greeks` does this for any model), or anchor the skew to a fixed
   reference.
4. **The signals come from a model that was never fitted.** Cell 4 calibrates the 4-parameter analytical Merton,
   then prices with the PDE model, whose two extra parameters are fixed by hand (`s_space = 0.10`, `l_mom = 0.20`).
   The gap between market and model is then partly the difference between two models.
5. **Parameters unpacked in the wrong order** (cell 7): the optimiser's vector is
   `[s0, s_space, l0, l_mom, mu_j, sigma_j]`, but the result is unpacked as `opt_s0, opt_lamb, opt_mu_j, opt_sigma_j,
   opt_s_space, opt_l_mom`. The production pricing pass then runs with the skew as the jump intensity, and so on.
6. **Mark prices aren't tradable prices, and a 14% MAPE is a poor fit.** A ±3% band around mark is inside Deribit's
   spread and fees for most strikes. Check the gaps against bid and ask, after fees, and only with a model that
   passes section 4.

## 7. Open decisions

- **Deribit account and API keys** (options trading and the testnet): add them to the "Needs exchange API keys"
  list in `TODO.MD`. Check that Deribit serves Norwegian residents.
- **Settlement:** BTC-settled inverse options (the deepest market; collateral and P&L in BTC, so the book needs a
  BTC venue currency) or USDC-settled linear options (simpler accounting, less liquid). Default: start with the
  inverse contracts for data and research; decide before O7.
- **Which strategy first:** protective structures (covered calls and tail hedges on the trend book) are the easiest
  to reason about and to size by exposure. Volatility selling needs O4's premium study first.
- **Data:** record from now (free, starts from zero), or buy history (e.g. Tardis.dev) for an immediate multi-year
  backtest.
