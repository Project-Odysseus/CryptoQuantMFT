# Data inventory: what mechanism-driven research needs, what we have, what to get

Research starts from a mechanism ("who is forced or paid to trade, and does that move prices?"), then measures
features (IC by horizon, `scripts/research/feature_decay.py`), and only then builds rules. This file maps each
mechanism to the data it needs. Status 2026-09-27. Everything is subject to the frozen final holdout
(`src/research/governance.py`): research sees data up to 2025-12-31.

## What we have

| Data | Coverage | Where | Loader |
| --- | --- | --- | --- |
| Kraken perp candles (trade, with volume) | BTC, ETH: 15m/1h/4h/1d from 2020-02; 1m for the last 90 days | `data/historical_cache/kraken_futures_*` | `load_bars(..., source="perp")` |
| Kraken spot candles | BTC/ETH/SOL-EUR 4h and 1d; intraday only the last 720 candles | `data/historical_cache/*-EUR_*` | `load_bars(...)` |
| Binance USD-M archive, daily | 859 coins, current and delisted (survivorship-free): klines with taker-buy volume and trade count, funding, premium index (basis) | `data/historical_cache/binance_um/` | `binance_archive.load_panel` |
| Binance intraday klines | 10 liquid coins, 3m, perps and spot, June-August 2026 (inside the frozen window) | `binance_um/klines_3m/` | `binance_archive.load_klines` (any coin, 1m-1h, on demand) |
| Positioning, BTC and ETH | Binance funding (2019-), hourly taker flow, 5-minute open interest, top-trader and account long/short ratios (2021-11-); Bybit funding and OI; Deribit funding and DVOL implied vol (2021-03-) | `data/historical_cache/positioning/` | `positioning.load_positioning` |
| Kraken funding | hourly rates, full history | public API | `kraken_futures.fetch_funding_history` |
| Deribit option chains | one snapshot (2026-09-26), BTC and ETH | `data/options/deribit/` | `options.deribit.load_chains` |
| Recorder | one day (2026-09-26): Kraken Futures trades, top-10 book, ticker; Kraken spot; Binance and Bybit liquidations | `data/market_data/` | `recorder.load_market_data` |
| FX | Norges Bank EUR/NOK, USD/NOK | SQLite | `FXRateCollector` |
| Our own orders and fills | every paper, dry-run and live fill with decision reason | SQLite `trades`, `operational_events` | `TradeLogger` |

## By mechanism: what each study needs

| Mechanism | Example question | Data needed | Have? | How to get it |
| --- | --- | --- | --- | --- |
| **Crowding / funding resets** (forced flow) | Do funding extremes and crowded long/short ratios reverse? | funding, OI, long/short ratios per coin | BTC, ETH yes; alts: funding yes (859 coins), OI and ratios no | Binance archive `metrics` per symbol (5-minute OI and ratios since 2021-11), free, ~5-10 MB per coin: extend `positioning.py` to the 30-50 coins Kraken lists |
| **Liquidation cascades** (forced flow) | What happens after the biggest liquidation hours? | liquidation prints with size and side | 1 day recorded | No venue publishes history: **start the recorder** on the always-on machine (Binance, Bybit, Kraken streams). Paid history: Coinalyze, CoinGlass, Tardis |
| **Options expiry and dealer hedging** | Does price pin near max open interest strikes into Friday 08:00 UTC expiries? | OI per strike and expiry over time, expiry calendar | 1 chain snapshot; calendar is derivable | **Record chains hourly** (`--record-option-chains`, ~5 MB a day). Paid history: Tardis, Deribit's historical data |
| **Volatility risk premium** (risk premium) | Is implied vol above later realized vol, by tenor? | DVOL or chains, realized vol | DVOL yes (2021-), realized yes | Enough for study O4 now |
| **Funding carry** (risk premium) | Is basis/funding worth harvesting per coin? | funding, premium index, spot and perp prices | yes (859 coins daily) | Study done for BTC/ETH; altcoin carry is ready to run |
| **Cross-sectional momentum and flow** (behavioral) | Do coins with strong taker buying keep outperforming? | daily klines and taker volume for many coins, survivorship-free | yes | Taker-buy book already found (deflated Sharpe 0.94); needs basket sleeves to trade |
| **Short-term reversal** (behavioral / liquidity) | Does a sharp 1h move revert? | intraday candles | yes | Measured: real (t -12) but ~1 bp per trade, below costs; only worth revisiting with maker execution |
| **ETF and institutional flows** (index flows) | Do US spot ETF net flows lead BTC returns? | daily ETF flows | no | Free daily tables (Farside Investors, issuers' sites); small |
| **Stablecoin mint/burn** (liquidity) | Does new USDT/USDC supply lead prices? | daily stablecoin supply | no | DefiLlama stablecoins API, free, daily, small |
| **Venue and region premia** (structural) | Does the Coinbase-over-Binance premium (US demand) predict? CME basis? | Coinbase and Binance prices, CME futures | Binance yes; Coinbase no; CME no | Coinbase public candles, free; CME daily settlements (delayed data, free) |
| **Time of day, weekends, events** (structural) | Do returns or vol differ around US open, weekends, CPI/FOMC? | intraday candles; an event calendar | candles yes; calendar no | A static CSV of CPI, FOMC and expiry dates, hand-made once |
| **Order-book imbalance, trade flow** (microstructure) | Does book imbalance predict the next minutes? | full trades, L2 book | 1 day recorded | Record continuously (several GB a day; needs disk and compression). Paid: Tardis |
| **On-chain exchange flows** | Do coins moving onto exchanges precede selling? | exchange in/outflows | no | Paid (CryptoQuant, Glassnode); postpone |

## What to get, in order

1. **Start the recorders** on the always-on machine: liquidations, option chains, top-of-book. Free, and history
   can't be bought back cheaply later. (Needs `TODO.MD` section 3.)
2. **Binance `metrics` for the Kraken-listed coins**: OI and long/short ratios since 2021-11 extend the crowding
   study (the one feature that clears costs) from 2 coins to 30-50. Free, a few hundred MB.
3. **Small free daily series**: US spot ETF flows, stablecoin supply, the Coinbase premium, an event calendar.
   Each is a few KB a day and a new source of information (not another price transformation).
4. **Paid only when a free-data study says it's worth it**: historical liquidations or L2 books (Tardis), option
   chain history.

## Paper vs backtest (not a download)

The gap between paper fills and a backtest over the same bars is the real cost model. The data for it is already
written: every paper and live fill with its decision price is in SQLite. It needs a few weeks of paper trading
and a comparison script (`TODO.MD` section 5).
