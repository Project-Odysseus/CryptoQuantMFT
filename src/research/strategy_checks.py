"""Is a strategy's return an edge or a fluke? The same checks for every strategy, on one card.

The signal card (`signal_checks.py`) screens a signal before any rules exist. This card judges the finished
strategy (signal + rules + costs) from its net return series, and answers the questions that decide whether a
backtest result is real:

- **Is the mean return different from zero?** Newey-West t-statistic (robust to autocorrelated returns) and the
  Sharpe ratio with a circular block-bootstrap interval (robust to volatility clusters and fat tails).
- **Is it more than the best of many tries?** Deflated Sharpe ratio against every configuration in the study and
  the hypothesis's trial ledger; Romano-Wolf adjusted p-values across the variants (all compared at once, allowing
  for how correlated they are); probability of backtest overfitting from a parameter sweep (CSCV).
- **Is it stable?** Each half, each calendar year, without its best year, and a rolling one-year Sharpe.
- **Does the timing matter?** With positions, a placebo shifts them in time against the same market returns.
- **Is it better than the baseline?** A paired block bootstrap of the Sharpe difference (same days resampled for both).
- **How good is it really?** A Bayesian shrinkage of the Sharpe toward a sceptical prior, and the probability that
  it is above zero and above the baseline. Reported, never a gate: the frequentist checks decide.

    from src.research import strategy_checks as stc
    card = stc.strategy_card(net, name="H13 P", baseline=net_b1, variants=all_variants, family="H13")
    card.table, card.verdict        # "evidence of an edge", "weak", "no evidence" or "short history"
    card.save()

Pass marks are in `research/strategy_standard.toml`, fixed in advance. Returns inside the frozen final holdout are
cut off first, and each card is noted in the trial ledger (it adds no trials: the study logs those).
"""

from __future__ import annotations

import hashlib
import itertools
import math
import tomllib
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, norm, skew

from src.research import governance
from src.research.inference import circular_shifts, family_trials
from src.research.signal_checks import newey_west_mean
from src.research.stats import deflated_sharpe_ratio, probabilistic_sharpe_ratio

STANDARD_PATH = Path("research/strategy_standard.toml")
CARD_DIR = Path("data/research/strategy_cards")
VERDICTS = ("evidence of an edge", "weak", "no evidence", "short history")


@dataclass(frozen=True, slots=True)
class StrategyStandard:
    """The pass marks and settings (see `research/strategy_standard.toml` for what each means)."""

    version: int = 1
    level: float = 0.95
    t_min: float = 2.0
    dsr_min: float = 0.95
    stable_share: float = 0.60
    min_blocks: int = 3
    best_block_keep: float = 0.5
    placebo_p: float = 0.05
    pbo_max: float = 0.5
    min_years: float = 3.0
    family_level: float = 0.05
    bootstrap_block: int = 60
    bootstrap_runs: int = 2000
    placebo_runs: int = 500
    placebo_min_shift: int = 60
    cscv_blocks: int = 16
    prior_mean: float = 0.0
    prior_sd: float = 0.5


def load_standard(path: Path | str = STANDARD_PATH) -> StrategyStandard:
    """The standard from its file. An unknown key is an error, so a typo cannot loosen a check without notice."""
    raw = tomllib.loads(Path(path).read_text())
    unknown = sorted(set(raw) - {item.name for item in fields(StrategyStandard)})
    if unknown:
        raise ValueError(f"{path}: unknown keys {unknown}")
    return StrategyStandard(**raw)


# --- statistics ------------------------------------------------------------------------------------------------------

def annual_sharpe(returns: np.ndarray, periods_per_year: float) -> float:
    """Mean over standard deviation, annualised; NaN without spread."""
    values = np.asarray(returns, dtype=float)
    spread = values.std(ddof=1) if len(values) > 1 else 0.0
    return float(values.mean() / spread * math.sqrt(periods_per_year)) if spread > 0 else float("nan")


def newey_west_lags(count: int) -> int:
    """The usual automatic lag choice, floor(4 (T/100)^(2/9))."""
    return max(1, int(4.0 * (count / 100.0) ** (2.0 / 9.0)))


def hac_t(returns: np.ndarray) -> tuple[float, float]:
    """(t-statistic of the mean, ratio of the Newey-West to the plain standard error)."""
    mean, error = newey_west_mean(returns, newey_west_lags(len(returns)))
    plain = np.std(returns, ddof=1) / math.sqrt(len(returns))
    return (float(mean / error) if error and error > 0 else float("nan")), (float(error / plain) if plain > 0 and error else 1.0)


def bootstrap_sharpes(frame: pd.DataFrame, *, block: int, runs: int, seed: int, periods_per_year: float, chunk: int = 100) -> np.ndarray:
    """Annual Sharpe of every column on circular block-bootstrap resamples: runs x columns.

    Each resample draws the same rows for every column, so comparisons between strategies keep each day's returns
    together. Done in chunks of runs, so a long history does not need the whole runs x periods array at once.
    """
    values = frame.to_numpy(dtype=float)
    count = len(values)
    rng = np.random.default_rng(seed)
    block = max(1, min(block, count))
    blocks = int(math.ceil(count / block))
    out = np.empty((runs, values.shape[1]))
    for start in range(0, runs, chunk):
        size = min(chunk, runs - start)
        starts = rng.integers(0, count, size=(size, blocks))
        indices = ((starts[:, :, None] + np.arange(block)[None, None, :]).reshape(size, -1)[:, :count]) % count
        samples = values[indices]  # size x count x columns
        mean, spread = samples.mean(axis=1), samples.std(axis=1, ddof=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[start:start + size] = np.where(spread > 0, mean / spread * math.sqrt(periods_per_year), np.nan)
    return out


def romano_wolf(observed: np.ndarray, boot: np.ndarray) -> np.ndarray:
    """Romano-Wolf step-down adjusted p-values for H0: statistic <= 0, one per column.

    `observed` are the statistics, `boot` their bootstrap draws (runs x columns). Each is studentised by its bootstrap
    standard deviation; the draws are centred at the observed values so they show what luck alone gives. Taking the
    maximum across the remaining hypotheses at each step accounts for every variant tried and for how alike they are.
    """
    spread = np.nanstd(boot, axis=0, ddof=1)
    spread = np.where(spread > 1e-12, spread, np.nan)  # a column with no spread (the baseline against itself) is no hypothesis
    t_observed = observed / spread
    t_null = (boot - observed) / spread
    testable = [column for column in np.argsort(-np.nan_to_num(t_observed, nan=-np.inf)) if np.isfinite(t_observed[column])]
    adjusted = np.full(len(observed), np.nan)
    running = 0.0
    for step, column in enumerate(testable):
        worst = np.nanmax(t_null[:, testable[step:]], axis=1)
        running = max(running, float(np.mean(worst >= t_observed[column])))
        adjusted[column] = running
    return adjusted


def pbo_cscv(sweep: pd.DataFrame, *, blocks: int = 16, periods_per_year: float = 252.0) -> dict[str, Any]:
    """Probability of backtest overfitting by combinatorially symmetric cross-validation (Bailey et al., 2016).

    The history is cut into `blocks` pieces. For every way of picking half of them as "in-sample", the configuration
    with the best in-sample Sharpe is found and its rank out-of-sample is noted. PBO is the share of splits in which
    that winner ranks in the bottom half out-of-sample: near 0.5 means choosing the best was no better than chance.
    """
    values = sweep.dropna().to_numpy(dtype=float)
    configs = values.shape[1]
    if configs < 2 or blocks % 2:
        raise ValueError("a sweep needs at least two configurations and an even number of blocks")
    size = len(values) // blocks
    if size < 5:
        raise ValueError("too little history for that many blocks")
    pieces = values[: size * blocks].reshape(blocks, size, configs)
    sums, squares = pieces.sum(axis=1), (pieces ** 2).sum(axis=1)
    combos = np.array([[index in chosen for index in range(blocks)] for chosen in itertools.combinations(range(blocks), blocks // 2)], dtype=float)

    def sharpe(selection: np.ndarray) -> np.ndarray:
        count = selection.sum(axis=1, keepdims=True) * size
        mean = selection @ sums / count
        variance = (selection @ squares / count - mean ** 2) * count / (count - 1)
        return mean / np.sqrt(np.maximum(variance, 1e-18)) * math.sqrt(periods_per_year)

    inside, outside = sharpe(combos), sharpe(1.0 - combos)
    best = np.argmax(inside, axis=1)
    ranks = (outside.argsort(axis=1).argsort(axis=1) + 1)[np.arange(len(best)), best]  # 1 = worst out-of-sample
    relative = ranks / (configs + 1.0)
    logits = np.log(relative / (1.0 - relative))
    return {"pbo": float(np.mean(logits <= 0.0)), "splits": len(combos), "logits": logits,
            "median_oos_rank": float(np.median(relative)), "best_oos_sharpe": float(np.median(outside[np.arange(len(best)), best]))}


def timing_placebo(positions: pd.Series | pd.DataFrame, asset_returns: pd.Series | pd.DataFrame, *, runs: int, min_shift: int,
                   periods_per_year: float, seed: int) -> dict[str, Any]:
    """The real gross Sharpe against the same positions shifted in time (same exposure and trades, no timing).

    For a book, pass DataFrames with one column per market: every market's positions move by the same shift, so the
    book keeps its mix and only the timing against the market returns is broken.
    """
    held_frame, market_frame = (frame.to_frame() if isinstance(frame, pd.Series) else frame for frame in (positions, asset_returns))
    index = held_frame.index.intersection(market_frame.index)
    held = held_frame.loc[index].fillna(0.0).to_numpy(dtype=float)
    market = market_frame.loc[index, list(market_frame.columns)].fillna(0.0).to_numpy(dtype=float)
    if held.shape != market.shape:
        raise ValueError("positions and asset_returns need the same columns")
    real = annual_sharpe((held * market).sum(axis=1), periods_per_year)
    shifts = circular_shifts(len(held), runs, min_shift=min(min_shift, max(1, len(held) // 4)), seed=seed)
    placebos = np.array([annual_sharpe((np.roll(held, int(shift), axis=0) * market).sum(axis=1), periods_per_year) for shift in shifts])
    return {"real": real, "placebos": placebos, "p": float((1 + np.sum(placebos >= real)) / (1 + len(placebos)))}


def sharpe_standard_error(returns: np.ndarray, periods_per_year: float, hac_ratio: float = 1.0) -> float:
    """Annual Sharpe's standard error, allowing for skew and fat tails (Mertens) and for autocorrelation (`hac_ratio`)."""
    count = len(returns)
    per_period = float(np.mean(returns) / np.std(returns, ddof=1))
    gamma3, gamma4 = float(skew(returns)), float(kurtosis(returns, fisher=False))
    variance = max(1e-12, 1.0 - gamma3 * per_period + (gamma4 - 1.0) / 4.0 * per_period ** 2) / max(count - 1, 1)
    return math.sqrt(variance * periods_per_year) * max(hac_ratio, 1.0)


def shrink(estimate: float, error: float, prior_mean: float, prior_sd: float) -> tuple[float, float]:
    """Normal-normal posterior (mean, sd) of a true Sharpe given an estimate with `error` and the prior."""
    precision = 1.0 / prior_sd ** 2 + 1.0 / error ** 2
    mean = (prior_mean / prior_sd ** 2 + estimate / error ** 2) / precision
    return float(mean), float(math.sqrt(1.0 / precision))


# --- the card --------------------------------------------------------------------------------------------------------

@dataclass(slots=True)
class StrategyCard:
    """The checks of one strategy's net returns.

    Attributes:
        table: One row per check: the value, what it is compared with, and whether it passes (blank when the number
            is only reported or the check needs an input that was not given).
        variants: Every variant's Sharpe, raw and Romano-Wolf adjusted p-values against zero (and the baseline).
        verdict, reason: The reading, and why in one line.
        versus_baseline: "better", "not shown better" or "no baseline".
        details: The series behind the plots.
    """

    name: str
    periods_per_year: float
    table: pd.DataFrame
    variants: pd.DataFrame
    verdict: str
    reason: str
    versus_baseline: str
    standard: StrategyStandard
    details: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""

    def __repr__(self) -> str:
        return f"StrategyCard({self.name!r}: {self.verdict}; {self.reason}; baseline: {self.versus_baseline})"

    def to_markdown(self) -> str:
        """The card as a markdown page."""
        shown = self.table.copy()
        shown["passes"] = shown["passes"].map({True: "pass", False: "FAIL"}).fillna("")
        rows = ["| check | value | compared with | |", "| --- | --- | --- | --- |"]
        rows += [f"| {check} | {row['value']} | {row['compared with']} | {row['passes']} |" for check, row in shown.iterrows()]
        variant_rows = []
        if len(self.variants) > 1:
            columns = list(self.variants.columns)
            variant_rows = ["", "Variants (every configuration of the study; Romano-Wolf adjusts for all of them at once):", "",
                            "| variant | " + " | ".join(columns) + " |", "| --- |" + " --- |" * len(columns)]
            variant_rows += [f"| {index} | " + " | ".join(_fmt(value) for value in row) + " |" for index, row in self.variants.iterrows()]
        return "\n".join([f"# Strategy card: {self.name}", "", f"Standard version {self.standard.version}; {self.details.get('span', '')}.", "",
                          f"**{self.verdict}**: {self.reason}", f"Against the baseline: **{self.versus_baseline}**.", "", *rows, *variant_rows, ""])

    def plot(self) -> Any:
        """Six charts: growth, rolling one-year Sharpe, years, bootstrap and posterior, variants or PBO, drawdown."""
        import matplotlib.pyplot as plt

        figure, axes = plt.subplots(2, 3, figsize=(14, 7))
        growth = self.details["growth"]
        growth.plot(ax=axes[0, 0], title="Growth of 1")
        rolling = self.details["rolling_sharpe"]
        axes[0, 1].plot(rolling.index, rolling.to_numpy())
        axes[0, 1].fill_between(rolling.index, rolling - 2.0, rolling + 2.0, alpha=0.15)  # one year's Sharpe has a standard error near 1
        axes[0, 1].set_title("Rolling one-year Sharpe (band: about 2 standard errors)")
        years = self.details["yearly"]
        axes[0, 2].bar([str(year) for year in years.index], years.to_numpy() * 100.0)
        axes[0, 2].set_title("Return by year, %")
        axes[0, 2].tick_params(axis="x", rotation=70)
        ax = axes[1, 0]
        ax.hist(self.details["boot_sharpe"], bins=50, density=True, alpha=0.5, label="bootstrap Sharpe")
        grid = np.linspace(-2.0, 3.0, 300)
        ax.plot(grid, norm.pdf(grid, self.standard.prior_mean, self.standard.prior_sd), linestyle="--", label="prior")
        ax.plot(grid, norm.pdf(grid, *self.details["posterior"]), label="posterior")
        ax.set_title("Sharpe: bootstrap, prior and posterior")
        ax.legend(fontsize=7)
        if "pbo_logits" in self.details:
            axes[1, 1].hist(self.details["pbo_logits"], bins=40)
            axes[1, 1].axvline(0.0, color="black", linewidth=0.8)
            axes[1, 1].set_title("CSCV: best in-sample, rank out-of-sample (logit)")
        else:
            variants = self.variants["Sharpe"]
            axes[1, 1].bar([str(index) for index in variants.index], variants.to_numpy())
            axes[1, 1].set_title("Sharpe by variant")
            axes[1, 1].tick_params(axis="x", rotation=60)
        drawdown = growth.iloc[:, 0] / growth.iloc[:, 0].cummax() - 1.0
        axes[1, 2].fill_between(drawdown.index, drawdown.to_numpy() * 100.0, 0.0, alpha=0.5)
        axes[1, 2].set_title("Drawdown, %")
        for ax in (axes[0, 1], axes[0, 2]):
            ax.axhline(0.0, linewidth=0.8, color="black")
        figure.suptitle(f"{self.name}: {self.verdict}")
        figure.tight_layout()
        return figure

    def save(self, directory: Path | str | None = None) -> Path:
        """Write the page, the tables and the charts; returns the folder."""
        out = Path(directory) if directory else CARD_DIR / f"{_slug(self.name)}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "card.md").write_text(self.to_markdown())
        self.table.to_csv(out / "table.csv")
        self.variants.to_csv(out / "variants.csv")
        figure = self.plot()
        figure.savefig(out / "card.png", dpi=120)
        import matplotlib.pyplot as plt

        plt.close(figure)
        return out


def _slug(name: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in name.lower()).strip("_") or "strategy"


def _fmt(value: Any, digits: int = 2) -> str:
    if isinstance(value, (float, np.floating)):
        return "–" if not np.isfinite(value) else f"{value:.{digits}f}"
    return str(value)


def _trim(frame: pd.DataFrame) -> pd.DataFrame:
    return governance.trim_frame(frame, label="strategy card") if len(frame) else frame


def _row(value: Any, compared: str = "", passes: bool | None = None) -> dict[str, Any]:
    return {"value": value if isinstance(value, str) else _fmt(value), "compared with": compared, "passes": passes}


def strategy_card(returns: pd.Series, *, name: str, periods_per_year: float = 252.0, baseline: pd.Series | None = None,
                  variants: Mapping[str, pd.Series] | pd.DataFrame | None = None, positions: pd.Series | pd.DataFrame | None = None,
                  asset_returns: pd.Series | pd.DataFrame | None = None, sweep: pd.DataFrame | None = None, family: str | None = None,
                  standard: StrategyStandard | None = None, ledger: bool = True, seed: int = 0) -> StrategyCard:
    """Every check on one strategy's net returns (one row per period, after costs, above cash where that applies).

    Args:
        returns: The strategy's net returns, indexed by time.
        name: What the page and the ledger call it.
        periods_per_year: 252 for markets that close at weekends, 365 for crypto daily, and so on.
        baseline: What it has to beat (the current rule, buy-and-hold). Compared day by day on the same resamples.
        variants: Every other configuration the study ran, by name. They count as trials, and the Romano-Wolf p-values
            allow for all of them. `returns` is included automatically.
        positions, asset_returns: For the timing placebo: the position earning each period's market return (Series
            for one market, DataFrames with one column per market for a book).
        sweep: A parameter sweep's returns (periods x configurations) for the probability of backtest overfitting.
        family: The hypothesis (e.g. "H13"): its trial count in the ledger feeds the deflated Sharpe.
        standard: Pass marks; default `research/strategy_standard.toml`.
        ledger: Note the card in the trial ledger (adds no trials). Tests switch it off.
        seed: For the bootstrap and the placebo.
    """
    standard = standard or load_standard()
    named = {name: returns}
    if variants is not None:
        # the strategy itself may be among the variants under another name: it is one trial, not two
        named.update({str(key): series for key, series in variants.items() if str(key) != name and not series.equals(returns)})
    if baseline is not None:
        named.setdefault("__baseline__", baseline)
    frame = _trim(pd.concat(named, axis=1).dropna(how="all"))
    frame = frame[frame[name].notna()].fillna(0.0)
    primary = frame[name].to_numpy(dtype=float)
    count = len(primary)
    years = count / periods_per_year
    span = f"{count} periods, {frame.index[0]:%Y-%m-%d} to {frame.index[-1]:%Y-%m-%d}, {years:.1f} years" if count else "no data"
    rows: dict[str, dict[str, Any]] = {}

    if years < standard.min_years or count < 30:
        table = pd.DataFrame({"history": _row(f"{years:.1f} years", f">= {standard.min_years:g} years", False)}).T
        return StrategyCard(name, periods_per_year, table, pd.DataFrame(), "short history", f"{years:.1f} years of returns; at least {standard.min_years:g} needed",
                            "no baseline" if baseline is None else "not tested", standard, {"span": span})

    # Is the mean different from zero?
    sharpe = annual_sharpe(primary, periods_per_year)
    t_stat, hac_ratio = hac_t(primary)
    variant_cols = [column for column in frame.columns if column != "__baseline__"]
    all_boot = bootstrap_sharpes(frame, block=standard.bootstrap_block, runs=standard.bootstrap_runs, seed=seed, periods_per_year=periods_per_year)
    boot = all_boot[:, [list(frame.columns).index(column) for column in variant_cols]]
    boot_primary = boot[:, variant_cols.index(name)]
    tail = (1.0 - standard.level) / 2.0
    low, high = np.nanquantile(boot_primary, [tail, 1.0 - tail])
    rows["Mean return, Newey-West t"] = _row(t_stat, f">= {standard.t_min:g}", bool(t_stat >= standard.t_min))
    rows["Sharpe (annual), interval"] = _row(f"{sharpe:.2f} ({low:.2f} to {high:.2f})", f"{standard.level:.0%} interval above 0", bool(low > 0.0))

    # More than the best of many tries?
    per_period = frame[variant_cols].mean() / frame[variant_cols].std(ddof=1)
    ledger_trials, ledger_variance = family_trials(family) if family else (0, 0.0)
    trials = max(ledger_trials, len(variant_cols), 1)
    variance = max(float(np.var(per_period.to_numpy(), ddof=1)) if len(variant_cols) > 1 else 0.0, ledger_variance, 1.0 / count)
    dsr = deflated_sharpe_ratio(primary, trials=max(trials, 2), sharpe_variance=variance) if trials > 1 else probabilistic_sharpe_ratio(primary)
    rows["Deflated Sharpe"] = _row(dsr, f">= {standard.dsr_min:g}, {trials} trials", bool(dsr >= standard.dsr_min))
    rows["Probabilistic Sharpe (vs 0)"] = _row(probabilistic_sharpe_ratio(primary))
    observed = np.array([annual_sharpe(frame[column].to_numpy(), periods_per_year) for column in variant_cols])
    variant_table = pd.DataFrame({"Sharpe": observed}, index=variant_cols)
    variant_table["p vs 0"] = np.mean(boot - observed >= observed, axis=0)  # one-sided: the bootstrap centred on no edge
    variant_table["p vs 0, Romano-Wolf"] = romano_wolf(observed, boot)
    rows["Romano-Wolf p (vs 0, all variants)"] = _row(float(variant_table.at[name, "p vs 0, Romano-Wolf"]), f"< {standard.family_level:g}",
                                                      bool(variant_table.at[name, "p vs 0, Romano-Wolf"] < standard.family_level) if len(variant_cols) > 1 else None)
    details: dict[str, Any] = {"span": span, "boot_sharpe": boot_primary}
    if sweep is not None:
        result = pbo_cscv(_trim(sweep), blocks=standard.cscv_blocks, periods_per_year=periods_per_year)
        rows["Probability of backtest overfitting"] = _row(result["pbo"], f"<= {standard.pbo_max:g}, {sweep.shape[1]} configurations", bool(result["pbo"] <= standard.pbo_max))
        details["pbo_logits"] = result["logits"]
    else:
        rows["Probability of backtest overfitting"] = _row("no sweep given")

    # Stable?
    series = frame[name]
    half = count // 2
    halves = (annual_sharpe(primary[:half], periods_per_year), annual_sharpe(primary[half:], periods_per_year))
    rows["Sharpe, first / second half"] = _row(f"{halves[0]:.2f} / {halves[1]:.2f}", "both above 0", bool(min(halves) > 0.0))
    yearly = (1.0 + series).groupby(series.index.year).prod() - 1.0
    positive = float((yearly > 0).mean())
    rows["Years with a positive return"] = _row(f"{positive:.0%} of {len(yearly)}", f">= {standard.stable_share:.0%}",
                                                bool(positive >= standard.stable_share) if len(yearly) >= standard.min_blocks else None)
    best_year = int(yearly.idxmax())
    without = annual_sharpe(series[series.index.year != best_year].to_numpy(), periods_per_year)
    rows["Sharpe without the best year"] = _row(f"{without:.2f} (without {best_year})", f"same sign, >= {standard.best_block_keep:.0%} of {sharpe:.2f}",
                                                bool(np.sign(without) == np.sign(sharpe) and abs(without) >= standard.best_block_keep * abs(sharpe)))
    window = int(round(periods_per_year))
    rolling = series.rolling(window).mean() / series.rolling(window).std() * math.sqrt(periods_per_year)
    rows["Rolling one-year Sharpe below 0"] = _row(f"{float((rolling.dropna() < 0).mean()):.0%} of windows (worst {rolling.min():.2f}, best {rolling.max():.2f})")
    growth = (1.0 + series).cumprod()
    drawdown = growth / growth.cummax() - 1.0
    under = (drawdown < 0).astype(int)
    longest = int(under.groupby((under == 0).cumsum()).sum().max())
    rows["Worst drawdown, longest under water"] = _row(f"{drawdown.min():.1%}, {longest / periods_per_year:.1f} years")

    # Timing
    if positions is not None and asset_returns is not None:
        placebo = timing_placebo(positions, asset_returns, runs=standard.placebo_runs, min_shift=standard.placebo_min_shift, periods_per_year=periods_per_year, seed=seed)
        rows["Timing placebo p"] = _row(placebo["p"], f"<= {standard.placebo_p:g} (gross Sharpe {placebo['real']:.2f})", bool(placebo["p"] <= standard.placebo_p))
    else:
        rows["Timing placebo p"] = _row("no positions given")

    # Baseline and the Bayesian view
    error = sharpe_standard_error(primary, periods_per_year, hac_ratio)
    posterior = shrink(sharpe, error, standard.prior_mean, standard.prior_sd)
    rows["Shrunk Sharpe (posterior mean)"] = _row(f"{posterior[0]:.2f} ± {posterior[1]:.2f}", f"prior {standard.prior_mean:g} ± {standard.prior_sd:g}")
    rows["P(Sharpe > 0), posterior"] = _row(float(norm.cdf(posterior[0] / posterior[1])))
    versus = "no baseline"
    if baseline is not None:
        base = frame["__baseline__"].to_numpy(dtype=float)
        base_boot = all_boot[:, list(frame.columns).index("__baseline__")]
        difference = sharpe - annual_sharpe(base, periods_per_year)
        diff_boot = boot_primary - base_boot
        d_low, d_high = np.nanquantile(diff_boot, [tail, 1.0 - tail])
        p_better = float(np.mean(diff_boot - difference >= difference))
        rows["Sharpe minus baseline, interval"] = _row(f"{difference:+.2f} ({d_low:+.2f} to {d_high:+.2f})", f"{standard.level:.0%} interval above 0", bool(d_low > 0.0))
        rows["p (better than baseline)"] = _row(p_better)
        diff_post = shrink(difference, float(np.nanstd(diff_boot, ddof=1)), 0.0, standard.prior_sd)
        rows["P(Sharpe > baseline), posterior"] = _row(float(norm.cdf(diff_post[0] / diff_post[1])))
        boot_vs_base = boot - base_boot[:, None]
        observed_vs_base = observed - annual_sharpe(base, periods_per_year)
        variant_table["minus baseline"] = observed_vs_base
        variant_table["p vs baseline, Romano-Wolf"] = romano_wolf(observed_vs_base, boot_vs_base)
        versus = "better" if d_low > 0.0 else "not shown better"

    table = pd.DataFrame(rows).T
    gates = table["passes"].dropna()
    core = ["Mean return, Newey-West t", "Sharpe (annual), interval"]
    if not all(bool(table.at[check, "passes"]) for check in core):
        verdict, reason = "no evidence", f"Sharpe {sharpe:.2f}, t {t_stat:.2f}: the mean return is not distinguishable from zero"
    elif all(bool(value) for value in gates):
        verdict, reason = "evidence of an edge", "every check that applies passes"
    else:
        failed = [check for check, value in gates.items() if not bool(value)]
        verdict, reason = "weak", "the mean is significant but these fail: " + ", ".join(failed)
    details.update({"growth": growth.to_frame(name), "rolling_sharpe": rolling.dropna(), "yearly": yearly, "posterior": posterior})
    fingerprint = hashlib.sha256(pd.util.hash_pandas_object(frame, index=True).values.tobytes()).hexdigest()[:16]
    card = StrategyCard(name, periods_per_year, table, variant_table, verdict, reason, versus, standard, details, fingerprint)
    if ledger:
        _note(card, family)
    return card


def _note(card: StrategyCard, family: str | None) -> None:
    """One ledger note per card (not a trial: the study logs its trials); the same input is not noted twice."""
    study = f"strategy_card:{card.name}"
    if any(entry.get("study") == study and entry.get("fingerprint") == card.fingerprint for entry in governance.ledger_entries()):
        return
    governance._append({"type": "strategy_card", "study": study, "family": family or card.name, "fingerprint": card.fingerprint,
                        "verdict": card.verdict, "versus_baseline": card.versus_baseline, "standard_version": card.standard.version})
