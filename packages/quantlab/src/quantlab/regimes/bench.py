"""The regime test bench: how well does a detector find regimes that are known to exist? (plan 3.3)

`score(model, paths, mapping)` runs a regime model on simulated paths (quantlab.regimes.simulate) and compares its
labels with the truth, bar by bar, after the model's warm-up:

- coverage: the share of bars where the model makes a call (a state mapped to a truth state; e.g. TRANSITION is no call);
- accuracy on the bars it calls, and balanced accuracy (the mean of per-state recalls, fair when states are unequal);
- precision and recall per truth state;
- detection lag: for each true episode, bars from its start until the model first agrees (missed if never in it);
- false switches a year: model changes into a state that is not the truth at that bar;
- the spread of all of these across paths (stability).

`mapping` maps the model's states to truth states (None = no call). `bench(models, paths)` scores several models into
one table; `sweep(models, process, field, values)` repeats it as one setting of the simulated market changes (noise,
episode length, a structural break) to show how stable each detector is. Synthetic results say whether a detector *can* work; real-data cards (quantlab.validation.regime_card) say
whether it does.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from quantlab.regimes.models import labels
from quantlab.regimes.simulate import RegimeProcess, simulate_paths


def _episodes(truth: pd.Series) -> list[tuple[int, int, str]]:
    change = truth.ne(truth.shift()).to_numpy()
    starts = list(np.flatnonzero(change)) + [len(truth)]
    return [(starts[i], starts[i + 1], truth.iloc[starts[i]]) for i in range(len(starts) - 1)]


def score_path(predicted: pd.Series, truth: pd.Series, *, states: tuple[str, ...] = ("TREND", "RANGE")) -> dict[str, float]:
    """Scores of one path. `predicted` holds truth-state names, or None/NaN for no call."""
    first = predicted.first_valid_index()
    if first is None:
        return {}
    predicted, truth = predicted.loc[first:], truth.loc[first:]
    called = predicted.notna() & truth.isin(states)
    in_scope = truth.isin(states)
    out: dict[str, float] = {"coverage": float(called.sum() / max(in_scope.sum(), 1))}
    hits = (predicted[called] == truth[called])
    out["accuracy"] = float(hits.mean()) if called.any() else float("nan")
    recalls = []
    for state in states:
        is_state = truth == state
        said = predicted == state
        recall = float((said & is_state).sum() / max(is_state.sum(), 1))
        out[f"recall {state}"] = recall
        out[f"precision {state}"] = float((said & is_state).sum() / max(said.sum(), 1)) if said.any() else float("nan")
        recalls.append(recall)
    out["balanced accuracy"] = float(np.mean(recalls))
    lags, missed = [], 0
    pred = predicted.to_numpy(dtype=object)
    for start, end, state in _episodes(truth):
        if state not in states or end - start < 5:
            continue
        agree = np.flatnonzero(pred[start:end] == state)
        if len(agree):
            lags.append(int(agree[0]))
        else:
            missed += 1
    out["median lag (bars)"] = float(np.median(lags)) if lags else float("nan")
    out["episodes missed"] = float(missed / max(missed + len(lags), 1))
    last_call = predicted.ffill().shift()  # a switch: a call that differs from the previous call, no-call gaps skipped
    switches = predicted.notna() & last_call.notna() & (predicted != last_call)
    false_switch = switches & (predicted != truth)
    years = len(predicted) / 252.0
    out["switches a year"] = float(switches.sum() / years)
    out["false switches a year"] = float(false_switch.sum() / years)
    true_switches = truth.ne(truth.shift()).sum() - 1
    out["true switches a year"] = float(true_switches / years)
    return out


def score(model: object, paths: list[tuple[pd.DataFrame, pd.Series]], mapping: Mapping[str, str | None], *,
          states: tuple[str, ...] = ("TREND", "RANGE")) -> pd.DataFrame:
    """One row per path: the model's labels mapped to truth states, scored against the truth."""
    rows = []
    for bars, truth in paths:
        predicted = labels(model(bars)).map(lambda state: mapping.get(state) if isinstance(state, str) else None)  # type: ignore[operator]
        predicted = predicted.where(predicted.notna(), None)
        rows.append(score_path(predicted, truth, states=states))
    return pd.DataFrame(rows)


def bench(models: Mapping[str, tuple[object, Mapping[str, str | None]]], paths: list[tuple[pd.DataFrame, pd.Series]], *,
          states: tuple[str, ...] = ("TREND", "RANGE")) -> pd.DataFrame:
    """Mean and spread across paths of every score, one row per model."""
    table = {}
    for name, (model, mapping) in models.items():
        per_path = score(model, paths, mapping, states=states)
        means, spreads = per_path.mean(), per_path.std()
        table[name] = {**means.to_dict(), "balanced accuracy (std across paths)": float(spreads.get("balanced accuracy", np.nan))}
    return pd.DataFrame(table).T


def sweep(models: Mapping[str, tuple[object, Mapping[str, str | None]]], process: RegimeProcess, field: str, values: Sequence[object], *,
          metric: str = "balanced accuracy", years: int = 15, seeds: Sequence[int] = range(4)) -> pd.DataFrame:
    """One metric per model (columns) as one field of the simulated market takes each value (rows)."""
    rows = {}
    for value in values:
        paths = simulate_paths(replace(process, **{field: value}), years=years, seeds=list(seeds))
        rows[value] = bench(models, paths)[metric]
    return pd.DataFrame(rows).T.rename_axis(field)
