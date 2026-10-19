# quantlab conventions

Every component follows these. They are encoded in `src/quantlab/conventions.py` and tested; this document gives the
reasons. Changing one is a breaking change: bump the major version and say why in the changelog.

## Time

- **Storage.** Timestamps are stored in UTC.
- **What a bar's timestamp means.** A bar is indexed by the moment its data becomes final:
  - a **daily bar** by its exchange-local **session date** (the close of 2026-10-19 in New York is 2026-10-19);
  - an **intraday bar** by its **close time** in UTC.

  Adapters convert sources that stamp bars at their open (Kraken, the host's `OHLCVBar`) by adding the interval.
  *Why:* a decision "at bar t" can then use everything in row t and nothing after, with no off-by-one per source.
- **Calendars.** Each instrument carries an exchange calendar (`exchange_calendars` codes). Panels mixing instruments
  are aligned on the union of session dates. A market closed on a date contributes no new bar; its value is carried
  forward and flagged, never interpolated.

## Decision and execution

- **Decision.** A decision at bar t uses data up to and including bar t.
- **Default execution: `NEXT_CLOSE`.** The trade happens at the close of bar t+1 and the position earns from t+2. This
  is how a fund trading at the close works: the signal comes from today's settlement and the order goes in for
  tomorrow's close.
- **Alternative: `NEXT_OPEN`.** Trade at the open of t+1, where the data allows.
- **The engine enforces this, not each author.** A strategy cannot trade at the price that produced its signal.

## Returns

- **Features and statistics:** log returns. They are additive over time and more symmetric.
- **P&L:** simple returns. They add across instruments in a portfolio.
- **Futures:** P&L is the **excess return** over cash, since margin funds the position: price return plus whatever
  carry the price series does not already reflect, minus nothing for funding. Cash interest on idle equity is
  booked separately (the host book's `cash_rate`).
- **Index proxies** (an index standing in for a future): the excess return is index return + (dividend yield − cash
  rate) ÷ periods. Every report flags the proxy.

## Positions and sizing

- **Engine unit:** signed weight of equity in the base currency, `notional / equity`. A weight of 0.5 means long half
  the equity's worth of the instrument.
- **Contracts and lots** are derived only at the portfolio/execution layer (`weight_to_contracts`). Rounding is
  reported, because at small capital it changes the strategy.
- **Base currency:** a config field per portfolio (NOK for a Norwegian fund, USD for a crypto book). FX conversion
  happens in the cost/P&L layer.

## Signals, regimes, overlays

- **Signal:** a forecast with long-run standard deviation 1 and a cap of ±2 (`FORECAST_CAP`). The sign is the
  direction. A binary rule is a special case (±1).
  *Why:* signals from different families can then be averaged, compared and sized on one scale.
- **Regime model:** per-bar **probabilities** over named states, each row summing to 1
  (`check_regime_probabilities`). Labels are derived from probabilities, not the other way round, so a regime model
  can be scored like a forecast. Each regime model declares its decision lag.
- **Overlay:** maps signals plus regime probabilities to signal weights. It never sees returns after bar t.

## Annualisation

- **Exchange calendars:** 252 days a year; for intraday bars, a 6.5-hour regular session.
- **24/7 markets:** 365 days a year; for intraday bars, 24 hours.

`periods_per_year(interval, calendar)` is the one source.

## Configuration

- **Format.** TOML files parsed into frozen dataclasses (`quantlab.config`).
- **Unknown keys are errors**, missing required fields are errors, and every problem is reported at once.
- **Fingerprints.** `config_hash` identifies a config in the experiment store.

## Storage

- **Separate databases.** The lab's experiment store is its own database, separate from any trading database. The
  host's `data/cryptoquant.db` holds trades, the tax ledger and runtime events, and the lab never writes there.
  Research results live in the lab's store.
- **Dataset snapshots** are fingerprinted, and a study records the snapshots it used.

## Code

- **Language and style.** Python 3.11+, full type hints, and Google-style docstrings that explain *why*.
- **Dependencies.** Only numpy, pandas, scipy and matplotlib in the core. Anything else is an optional extra,
  imported lazily.
- **Charts.** matplotlib only. Never force a backend at import time: notebooks need their inline backend.
- **Boundary.** No imports from the host repository (enforced by `tests/test_boundary.py`).
