"""Lab-wide multiple-testing control: how many of the lab's "findings" are expected to be false? (plan 4.4)

Each card is a test. The deflated Sharpe and Romano-Wolf control the search inside one study; across the whole lab,
with dozens of ideas tested, some will pass by luck. The false discovery rate (FDR) is the expected share of false
ones among those called discoveries, and these procedures keep it at a chosen level:

- `benjamini_hochberg(p)`: q-values, valid under independence or positive dependence (Benjamini and Hochberg, 1995).
- `storey(p)`: q-values that also estimate the share of true nulls (pi0), more powerful when many ideas are real
  (Storey, 2002).
- `lab_fdr(level)`: every card in the ledger with its p-value, both q-values and whether it survives at `level`.
  A discovery is a card whose p survives; whether it is usable is still its verdict. The p noted is the card's
  test of the claim that matters: a strategy's timing (t and placebo), a signal's placebo, a regime's placebo.
- `dashboard()`: trials and cards per family, verdicts, the p-value histogram (a flat one means mostly noise).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from quantlab.validation import ledger


def benjamini_hochberg(p_values: np.ndarray | pd.Series) -> np.ndarray:
    """BH q-values (monotone, capped at 1), NaN where p is NaN."""
    p = np.asarray(p_values, dtype=float)
    out = np.full(len(p), np.nan)
    valid = np.flatnonzero(np.isfinite(p))
    if not len(valid):
        return out
    order = valid[np.argsort(p[valid])]
    count = len(order)
    ranked = p[order] * count / np.arange(1, count + 1)
    out[order] = np.minimum(1.0, np.minimum.accumulate(ranked[::-1])[::-1])
    return out


def storey(p_values: np.ndarray | pd.Series, *, lam: float = 0.5, min_tests: int = 20) -> tuple[np.ndarray, float]:
    """(Storey q-values, estimated share of true nulls pi0). With fewer than `min_tests` p-values pi0 cannot be
    estimated, so it is taken as 1 (plain BH)."""
    p = np.asarray(p_values, dtype=float)
    valid = p[np.isfinite(p)]
    if not len(valid):
        return np.full(len(p), np.nan), float("nan")
    if len(valid) < min_tests:
        return benjamini_hochberg(p), 1.0
    pi0 = float(min(1.0, np.mean(valid > lam) / (1.0 - lam)))
    return np.minimum(1.0, benjamini_hochberg(p) * max(pi0, 1.0 / len(valid))), pi0


def lab_fdr(level: float = 0.10, *, kind: str | None = None) -> pd.DataFrame:
    """The latest card of every idea with its p-value, BH and Storey q-values, and whether it is a discovery at `level`."""
    cards = ledger.latest_cards(kind)
    if not cards:
        return pd.DataFrame(columns=["kind", "family", "verdict", "p", "q (BH)", "q (Storey)", f"discovery at {level:g}"])
    table = pd.DataFrame({key: {"kind": row["kind"], "family": row.get("family"), "verdict": row["verdict"], "p": row.get("p")} for key, row in cards.items()}).T
    p = table["p"].astype(float).to_numpy()
    table["q (BH)"] = benjamini_hochberg(p)
    table["q (Storey)"], pi0 = storey(p)
    table[f"discovery at {level:g}"] = table["q (BH)"] <= level
    table.attrs["pi0"] = pi0
    return table.sort_values("p")


def dashboard(level: float = 0.10) -> Any:
    """Three charts: trials per family, card verdicts per family, and the p-values of every card."""
    import matplotlib.pyplot as plt

    summary = ledger.lab_summary()
    table = lab_fdr(level)
    figure, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    if len(summary):
        families = summary.drop(index="all")
        (families["trials"] if "trials" in families else pd.Series(dtype=float)).plot.barh(ax=axes[0], title="Trials per family")
        verdicts = families.drop(columns=[c for c in ("trials", "cards") if c in families.columns])
        if len(verdicts.columns):
            verdicts.plot.barh(stacked=True, ax=axes[1], title="Card verdicts per family")
    p = table["p"].astype(float).dropna() if len(table) else pd.Series(dtype=float)
    axes[2].hist(p, bins=20, range=(0, 1))
    discoveries = int(table[f"discovery at {level:g}"].sum()) if len(table) else 0
    axes[2].set_title(f"p-values of {len(p)} cards; {discoveries} discoveries at FDR {level:g}")
    figure.tight_layout()
    return figure
