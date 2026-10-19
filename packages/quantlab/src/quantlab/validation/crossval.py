"""Walk-forward and purged cross-validation for anything fitted (plan 4.2).

Anything with a parameter chosen from data (a lookback, a threshold, a model fit, a choice among variants) must be
judged on data it was not chosen on. Splits here are positions into an index (numpy arrays), so they work with any
frame.

- `walk_forward_splits`: expanding (`train=None`) or rolling (`train=n`) windows, refitted every `test` bars.
- `purged_kfold`: k contiguous test folds; training rows whose labels (spanning `horizon` bars) overlap a test fold
  are purged, and `embargo` bars after each fold are dropped too (López de Prado, 2018, ch. 7).
- `cpcv_splits` / `cpcv_paths`: combinatorial purged CV. `groups` blocks, `test_groups` of them tested at a time;
  the test predictions recombine into C(groups-1, test_groups-1) full backtest paths, a distribution of
  out-of-sample Sharpes instead of one.
- `select_walk_forward`: pick the best candidate on each training window by a metric, trade it on the next test
  window. The out-of-sample series is what choosing would really have earned; the choices show how stable the
  pick is. PBO (quantlab.validation.strategy_card.pbo_cscv) on the same candidates says whether picking helps.
- `walk_forward`: the general form: `evaluate(train_positions, test_positions) -> Series` per fold.
"""

from __future__ import annotations

import itertools
import math
from typing import Callable, Iterator, Mapping

import numpy as np
import pandas as pd

from quantlab.validation.strategy_card import pbo_cscv

Split = tuple[np.ndarray, np.ndarray]


def walk_forward_splits(count: int, *, test: int, train: int | None = None, min_train: int = 252, step: int | None = None) -> list[Split]:
    """(train, test) positions: test windows of `test` bars every `step` (= test) bars, trained on what came before."""
    step = step or test
    out = []
    for start in range(min_train, count, step):
        end = min(start + test, count)
        first = 0 if train is None else max(0, start - train)
        out.append((np.arange(first, start), np.arange(start, end)))
    return out


def _purge(train: np.ndarray, test: np.ndarray, *, horizon: int, embargo: int) -> np.ndarray:
    """Drop training rows whose label window [i, i + horizon) meets the test block, and an embargo after it."""
    low, high = test.min(), test.max()
    keep = (train + horizon - 1 < low) | (train > high + embargo)
    return train[keep]


def purged_kfold(count: int, *, folds: int = 5, horizon: int = 1, embargo: int = 0) -> list[Split]:
    """k contiguous test folds, each trained on the rest after purging and embargo."""
    blocks = np.array_split(np.arange(count), folds)
    out = []
    for block in blocks:
        rest = np.setdiff1d(np.arange(count), block)
        out.append((_purge(rest, block, horizon=horizon, embargo=embargo), block))
    return out


def cpcv_splits(count: int, *, groups: int = 6, test_groups: int = 2, horizon: int = 1, embargo: int = 0) -> list[tuple[np.ndarray, np.ndarray, tuple[int, ...]]]:
    """Every choice of `test_groups` of `groups` blocks as the test set: (train, test, which groups)."""
    blocks = np.array_split(np.arange(count), groups)
    out = []
    for chosen in itertools.combinations(range(groups), test_groups):
        train = np.arange(count)
        for group in chosen:  # purge around each test block separately (they need not touch)
            train = _purge(train, blocks[group], horizon=horizon, embargo=embargo)
        test = np.concatenate([blocks[group] for group in chosen])
        out.append((train, test, chosen))
    return out


def cpcv_paths(predictions: Mapping[tuple[int, ...], pd.Series], *, groups: int, test_groups: int, index: pd.Index) -> pd.DataFrame:
    """Recombine per-split test results into full paths (columns), each group's segment taken from a different split."""
    blocks = np.array_split(np.arange(len(index)), groups)
    paths = math.comb(groups - 1, test_groups - 1)
    used = {group: 0 for group in range(groups)}
    out = pd.DataFrame(np.nan, index=index, columns=[f"path {p + 1}" for p in range(paths)])
    for chosen in itertools.combinations(range(groups), test_groups):
        series = predictions[chosen]
        for group in chosen:
            rows = index[blocks[group]]
            out.loc[rows, f"path {used[group] + 1}"] = series.reindex(rows).to_numpy()
            used[group] += 1
    return out


def cpcv(evaluate: Callable[[np.ndarray, np.ndarray], pd.Series], index: pd.Index, *, groups: int = 6, test_groups: int = 2, horizon: int = 1,
         embargo: int = 0, periods_per_year: float = 252.0) -> dict[str, object]:
    """Run `evaluate(train, test)` on every CPCV split; return the paths and the distribution of their Sharpes."""
    predictions = {chosen: evaluate(train, test) for train, test, chosen in cpcv_splits(len(index), groups=groups, test_groups=test_groups,
                                                                                         horizon=horizon, embargo=embargo)}
    paths = cpcv_paths(predictions, groups=groups, test_groups=test_groups, index=index)
    sharpes = paths.mean() / paths.std() * math.sqrt(periods_per_year)
    return {"paths": paths, "sharpes": sharpes, "median Sharpe": float(sharpes.median()), "share of paths above 0": float((sharpes > 0).mean())}


def walk_forward(evaluate: Callable[[np.ndarray, np.ndarray], pd.Series], splits: list[Split]) -> pd.Series:
    """Concatenate `evaluate(train, test)` over the splits: one out-of-sample series."""
    return pd.concat([evaluate(train, test) for train, test in splits]).sort_index()


def _metric_sharpe(returns: pd.DataFrame) -> pd.Series:
    return returns.mean() / returns.std()


def select_walk_forward(candidates: pd.DataFrame, splits: list[Split], *, metric: Callable[[pd.DataFrame], pd.Series] = _metric_sharpe,
                        cscv_blocks: int = 16, periods_per_year: float = 252.0) -> dict[str, object]:
    """Choose the best candidate (column of returns) on each training window; trade it on the test window.

    Returns the out-of-sample series, the pick per fold, how often the pick changed, the in-sample-best-overall
    Sharpe for comparison, and the PBO of choosing among these candidates.
    """
    pieces, picks = [], []
    for train, test in splits:
        scores = metric(candidates.iloc[train].dropna(how="all"))
        best = str(scores.idxmax())
        picks.append({"test start": candidates.index[test[0]], "pick": best, "in-sample score": float(scores.max())})
        pieces.append(candidates.iloc[test][best])
    oos = pd.concat(pieces)
    picks_frame = pd.DataFrame(picks).set_index("test start")
    annual = math.sqrt(periods_per_year)
    out: dict[str, object] = {"oos": oos, "picks": picks_frame, "pick changes": int((picks_frame["pick"] != picks_frame["pick"].shift()).sum() - 1),
                              "oos Sharpe": float(oos.mean() / oos.std() * annual),
                              "best in hindsight Sharpe": float((candidates.mean() / candidates.std()).max() * annual)}
    try:
        out["pbo"] = pbo_cscv(candidates, blocks=cscv_blocks, periods_per_year=periods_per_year)["pbo"]
    except ValueError:
        out["pbo"] = float("nan")
    return out


def iter_folds(index: pd.Index, splits: list[Split]) -> Iterator[tuple[pd.Index, pd.Index]]:
    """The splits as index labels, for code that slices by date."""
    for train, test in splits:
        yield index[train], index[test]
