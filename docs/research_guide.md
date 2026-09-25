# Strategy research guide

How to test strategy ideas on real Kraken data, decide whether a result is real, and turn a good idea into
something the runtime can run. Findings from past runs are in [`research_log.md`](research_log.md).

Three ways in, all built on the same code:

| Entry point | Use it for |
| --- | --- |
| `notebooks/strategy_research.ipynb` | Exploring: single backtests with charts, writing your own strategy inline, sweeps, heatmaps |
| `python scripts/research/research.py ...` | Repeatable runs that save CSVs, a markdown summary and heatmaps |
| `from src.research import ...` | Your own scripts |

## Quick start

```bash
conda activate CryptoArb
python scripts/research/research.py list                              # every strategy, its hypothesis and grid
python scripts/research/research.py backtest keltner_breakout --symbol BTC/EUR --interval 1d --plot
python scripts/research/research.py backtest donchian_breakout --interval 4h --param entry_window=40 --param exit_window=10 --long-only
python scripts/research/research.py compare --intervals 4h 1d         # every strategy at its defaults
python scripts/research/research.py sweep --intervals 4h 1d           # full sensitivity sweep, ~1.5 min
python scripts/research/research.py sweep --strategies keltner_breakout --grid window=30,40,60 --grid atr_multiplier=1,1.5,2
python scripts/research/research.py sweep --maker                     # what limit (maker) orders would change
python scripts/research/research.py sweep --venue perp                # perpetual-futures fees + funding (assumed figures)
```

Useful flags on every command: `--symbols BTC/EUR ETH/EUR SOL/EUR`, `--sides long|long-short|both`,
`--holdout-fraction 0.3`, `--venue spot|perp`, `--maker`, `--fee-pct`, `--slippage-bps`, `--funding-pct-per-day`
(the last three override the venue preset), `--metric sharpe|return|consistency`,
`--csv SYMBOL=PATH`, `--refresh`, `--out DIR`.

`sweep` and `compare` write to `data/research/<command>_<timestamp>/`:
`results.csv` (one row per strategy × parameters × symbol × side), `summary.csv` + `summary.md` (one verdict per
strategy × interval × side), `config.json` (exact settings and dates) and `heatmaps/<interval>/*.png`.

## Where things live

| File | What it is |
| --- | --- |
| `src/backtest/strategies.py` | Every strategy factory, plus `latch_position`, `make_long_only`, `make_regime_gated` |
| `src/backtest/indicators.py` | Vectorized, causal indicators: rolling mean/std/max/min/quantile, ATR, RSI, slope t-stat |
| `src/backtest/runner.py` | `StrategyRegistry`: the names the runtime (`--strategy`) and research tools resolve |
| `src/backtest/simple_backtest.py` | The backtest engine (`SimpleBacktester`) |
| `src/research/catalog.py` | Research metadata per strategy: hypothesis, defaults, sweep grid |
| `src/research/engine.py` | Data loading, runs, in-sample/holdout metrics, `sweep`, `compare`, `summarize` |
| `src/research/plots.py` | `plot_heatmap`, `plot_run` |
| `src/data/historical.py` | Kraken OHLC download + parquet cache, CSV loader |

## How a strategy works

A strategy is a factory that takes parameters and returns a function
`strategy(history, index, current_bar) -> 1 | 0 | -1`. `history` is every bar up to and including the current one
(never future bars, so there is no look-ahead).

The return value is the **position to hold**, not an order. 1 = be long, -1 = be short, 0 = be flat. The engine
opens a position when the value turns non-zero and closes it as soon as the value is 0 or flips. A strategy that
wants to stay in a trade must keep returning 1 on every bar.

The paper and live runtime follow the same rule (aligned on 2026-09-25; before that the runtime treated 0 as
"hold" and only closed a long on -1). There is no size scaling yet, so 0 always means "exit whatever you hold".

That makes "enter on X, hold until Y" awkward to write by hand, so there is a helper. Compute indicator arrays over
the whole history, turn your rules into boolean arrays, and let `latch_position` decide:

```python
from src.backtest.indicators import rolling_max, rolling_min, series, shift
from src.backtest.strategies import latch_position

def my_breakout(entry_window: int = 20, exit_window: int = 10):
    def strategy(history, index, current_bar):
        if len(history) < entry_window + 1:
            return 0
        close, high, low = series(history, "close"), series(history, "high"), series(history, "low")
        return latch_position(
            long_entry=close > shift(rolling_max(high, entry_window)),   # close above the prior N-bar high
            long_exit=close < shift(rolling_min(low, exit_window)),      # close below the prior M-bar low
        )
    return strategy
```

`latch_position` says you are long if the most recent long entry is more recent than the most recent long exit (and
the same for short). An entry on one side counts as an exit for the other. It is recomputed from history every bar,
so there is no hidden state and it survives runtime restarts.

Indicators return NaN until they have enough history, and NaN comparisons are False, so rules are switched off during
warmup automatically. Use `shift(...)` to compare against the *prior* N bars (so the current bar can't move its own
threshold). Leave out the short arrays for a long-only strategy.

Two wrappers work on any strategy: `make_long_only(fn)` (shorts become flat, which is what spot live trading does
anyway) and `make_regime_gated(fn, required_regime="trending")`. In research they are the parameters
`long_only=True` and `regime="trending"`, which you can put in a sweep grid.

### Adding a strategy

1. Write the factory in `src/backtest/strategies.py` (full type hints, a docstring saying *why* it should work).
2. Register it in `StrategyRegistry` in `src/backtest/runner.py`. The runtime can then run it with
   `--strategy name --strategy-params '{...}'`.
3. Add a `StrategySpec` to `CATALOG` in `src/research/catalog.py` with a one-line hypothesis, defaults and a grid of
   the two parameters that matter most. `tests/test_research.py` builds every grid combo, so a typo fails the tests.
4. Add a test in `tests/test_strategies.py` showing it enters, holds and exits when it should.

You can research a strategy before step 1: pass a factory straight to `sweep(data, my_breakout, grid={...})` or a
built strategy to `run_strategy(bars, my_breakout(), label="...")`.

## How results are computed

These choices are deliberate. They are what makes the numbers comparable and hard to fool yourself with.

- **One continuous backtest per (strategy, parameters, symbol).** No resets part-way through, so trends aren't cut
  off and exits aren't forced at arbitrary points.
- **Trading costs on every fill.** The default is Kraken's taker fee for small accounts (0.40%) plus 10 bps of
  slippage, about **1.0% per round trip**. That hurdle decides most results (see the research log).
  `--venue perp` swaps in assumed perpetual-futures costs (0.05% taker + 5 bps, about 0.2% per round trip) and
  charges **funding** of 0.01% of notional per day on the open position (longs pay, shorts receive when positive).
  Funding is applied to the mark-to-market equity, so it shows in return, Sharpe and drawdown but not in
  `avg_trade` or `win_rate`. Fees are Kraken Futures' published entry tier and the funding default is close to
  the measured one-year mean for BTC and ETH; slippage is an assumption. Funding is volatile (negative about a
  third of the time, up to about +0.15%/day), so re-run with `--funding-pct-per-day 0.03` as a stress test.
- **Full size, no risk overlay.** Every trade uses 100% of equity with no leverage, stops or volatility sizing, so the
  numbers measure the signal. Sizing and stops are a separate layer that comes after a signal has earned it.
- **Mark-to-market equity.** Equity is valued at every bar's close, so drawdowns inside open trades count.
- **Trades execute at the close of the bar that produced the signal.** This is slightly optimistic. On 24/7 crypto
  markets the next open is essentially the same price, but a few bps of slippage are built into the cost default.
- **An in-sample / holdout split in time.** The first 70% of bars is in-sample (IS) and the last 30% is holdout (HO).
  Both are measured on the same equity curve, so a position spanning the boundary is split correctly.
  **Choose parameters by IS numbers only.** HO exists to check whether that choice holds up on data it never saw.
  Once you start picking parameters by HO results, HO becomes in-sample too, and you need fresh data to check again.
- **A common start date.** IS measurement starts at the longest warmup of everything in the run (e.g. bar 102), so
  every combination, every strategy and buy-and-hold are judged over the same dates.

Why not `main.py --walk-forward`? It resets the strategy every fold and credits a trade's whole P&L to the fold where
it closed, including gains made in the training window. With fixed parameters, a continuous run split into IS/HO
answers the same question without those distortions.

## Reading the output

### Per-run metrics (`results.csv`, `run.metrics_table()`), prefix `is_` or `ho_`

| Metric | Meaning |
| --- | --- |
| `return` | Total return over the period, net of costs |
| `sharpe` | Annualised Sharpe of bar-to-bar mark-to-market returns (365-day year, crypto trades 24/7) |
| `max_drawdown` | Worst peak-to-trough fall of the equity curve |
| `exposure` | Share of bars spent in a position |
| `trades`, `win_rate`, `avg_trade` | Trades closed in the period, share that made money, average return per trade after costs |
| `consistency` | Share of 6 equal sub-periods with a positive return (does it make money steadily or in one burst?) |
| `buy_hold_return`, `buy_hold_sharpe`, `buy_hold_max_drawdown` | Simply holding the coin over the same bars |

### Summary columns (`summary.csv`, `summarize()`)

Each parameter combo is first averaged across symbols. A setting has to work on more than one coin.

| Column | What it tells you | Good sign |
| --- | --- | --- |
| `share_positive_is` | Share of combos with positive IS Sharpe | High (>60%): the idea works across settings |
| `median_is`, `median_ho` | The typical combo. The most honest estimate for the strategy *family* | Both positive |
| `best_params`, `best_is` | The IS winner | - |
| `best_neighbors_is` | Median IS score one grid step from the winner | Close to `best_is` (plateau, not spike) |
| `best_ho` | What picking the IS winner actually delivered | Positive, not far below `best_is` |
| `rank_corr` | Does the IS ranking of combos carry over to HO? | Clearly positive, **but see the caveat below** |
| `buy_hold_is`, `buy_hold_ho` | Buy-and-hold Sharpe on the same dates | The bar to clear |

### Heatmaps

Colour is Sharpe, blue positive, red negative, averaged across symbols. The IS panel sits next to the HO panel. What
you want is a broad block of the same colour in-sample that is still that colour in the holdout. A single bright cell
surrounded by the opposite colour is a spike: someone got lucky with those exact numbers.

### Red flags

- **It only works at zero cost.** The signal may be real, but it isn't tradable at our fee tier. Compare runs with
  `--fee-pct 0 --slippage-bps 0` against the defaults. Divide `avg_trade` by the round-trip cost: below 1 means
  fees eat everything.
- **It beats zero but not buy-and-hold.** A long-only strategy that is in the market half the time in a rising
  market will show a positive Sharpe just from market exposure. Compare against `buy_hold_*`, and look at drawdown
  as well as return.
- **The best cell is far from its neighbours** (`best_neighbors_is` ≪ `best_is`), or `best_ho` is far below
  `median_ho`. That's an overfit spike.
- **`rank_corr` is high in a losing family.** When costs dominate, the combos that trade least lose least in both
  periods, which makes rank correlation high without any signal. Check the correlation between `is_trades` and
  `is_sharpe` before trusting it.
- **A handful of trades.** 3–5 trades per symbol in a period is anecdote, not statistics.
- **Correlated symbols aren't independent evidence.** BTC, ETH and SOL move together, so "works on all three" is
  weaker than it sounds.

## A research loop that works

1. Write the hypothesis in one sentence (why would this make money, and who is on the other side?).
2. `compare` at sensible defaults on 4h and 1d. Is it anywhere near positive net of costs? If not, check it at zero
   cost to see whether any signal exists at all.
3. `sweep` two parameters. Look for a plateau, a decent `share_positive_is`, and a holdout that agrees.
4. Judge against buy-and-hold on both Sharpe and drawdown. Be honest about which regime the periods covered.
5. Pick parameters from the *middle* of the plateau, not the best cell.
6. Before paper trading, re-test on more history (see below) if at all possible.
7. Paper trade (`--runtime paper`), then `live_dry_run`, then live with minimum size.

Record what you tried and what you found in `research_log.md`, including the ideas that failed, so they don't get
re-tested by accident.

## Getting more history

For BTC/USD and ETH/USD perpetuals, `--source perp` (or `load_bars(symbol, interval, source="perp")`) loads Kraken
Futures trade candles from 2020-02-26 to now, cached in `data/historical_cache/`, at any of 1m, 5m, 15m, 30m,
1h, 4h, 12h, 1d. That is the easiest route to multi-year history.

Kraken's public OHLC endpoint returns only the most recent 720 candles per pair and interval: 30 days at 1h,
120 days at 4h, about 2 years at 1d. `since` does not unlock older data. For multi-year history, download Kraken's
published OHLCVT CSV files (full history per pair and interval, on Kraken's support site under downloadable
historical OHLCVT data) and point the tools at them:

```bash
python scripts/research/research.py sweep --intervals 4h --csv BTC/EUR=~/Downloads/XBTEUR_240.csv --csv ETH/EUR=~/Downloads/ETHEUR_240.csv --symbols BTC/EUR ETH/EUR
```

```python
bars = load_bars("BTC/EUR", "4h", csv_path="~/Downloads/XBTEUR_240.csv")
```

The loader takes headerless `timestamp, open, high, low, close, volume[, trades]` rows (Kraken's format) or a header
row naming those columns. `--lookback-days` trims it.

## Known limitations

- **Short samples.** 4h data covers about 4 months (one regime). Daily covers about 2 years. More history is the
  single biggest improvement to confidence.
- **Per-symbol, not portfolio.** Each symbol is traded alone with 100% of equity. Combining strategies and symbols
  is the portfolio stage.
- **No stop-loss research yet.** Research runs have no risk overlay. The engine now keeps a stopped-out side flat
  until the signal resets, so stops could be added to research runs as a next step.
- **Running a researched timeframe.** The runtime can now trade the same bars the research used:
  `--bar-interval 4h` (or `1d`, ...) builds bars of that length regardless of the polling interval, the strategy
  only acts when a bar completes, and `--warmup-bars 200` loads recent history at startup so long windows work
  from the first cycle. Kraken spot OHLC only goes back 720 candles, which caps warmup at 30 days of 1h bars.
