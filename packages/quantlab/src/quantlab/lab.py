"""The notebook interface: test an idea in five lines, see it against and inside the book (plan Phase 6).

    from quantlab.lab import Lab
    lab = Lab(universe="equity_index_core", start="1999")
    idea = lab.signal(lambda px: (px.close.rolling(20).mean() / px.close.rolling(100).mean() - 1).rank(axis=1, pct=True) - 0.5)
    lab.test(idea)                                    # signal card, strategy card, curves, costs, versus the book's signals
    lab.combine(idea, "trend", overlay="h14_regime")  # the idea and trend switched by H14's regime, against its benchmarks
    lab.compare_with_book(idea)                       # correlation, marginal Sharpe and drawdown, diversification

What happens underneath, so results mean the same thing everywhere:

- **Data.** `universe` names a set of instruments in the master (`UNIVERSES`) or lists them; bars come from the data
  catalog (no download for snapshots already there; `scripts/lab/import_research_cache.py` fills it from this
  project's caches). Returns are excess of cash where the master says so; costs come from the master. Pass
  `bars={name: frame}` instead to work on any data (then returns are price returns and costs `cost_bps`).
- **Signals.** `lab.signal(fn)` takes a function of `px` (`px.close`, `px.high`, ... as dates x instruments frames),
  a library signal, or `per_instrument=True` for a function of one instrument's bars. The output is checked for
  hindsight (later data must not change earlier values: `.rank(pct=True)` down a column fails, `.rank(axis=1)`
  across markets passes) and put on the forecast scale (divided by its own trailing root-mean-square).
- **Trading.** Each instrument's forecast is sized to `target_vol` and weighted 1/N; decisions at the close trade at
  the next close (the engine). The book's signals (`BOOK`) are built the same way, so comparisons are like for like.
- **Exploratory or confirmatory.** Without `hypothesis=`, every call is exploratory: recorded in the experiment store
  under "EXPLORATORY" and counted there, but not a trial and not in the lab-wide false-discovery count. With
  `hypothesis="H15"`, the pre-registration must be locked (quantlab.validation.ledger), each test counts as a trial of
  that family, cards go to the ledger, and the frozen holdout opens only through `lab.final_look(...)`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import math
import os
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import numpy as np
import pandas as pd

from quantlab.analytics.performance import metrics
from quantlab.data.panel import Panel, align, load_panel
from quantlab.engine.backtest import BacktestResult, EngineConfig, Sizer, run_backtest
from quantlab.engine.components import Targets, apply_signal
from quantlab.signals import library as lib

UNIVERSES: dict[str, tuple[str, ...]] = {
    "equity_index_core": ("idx:SPX", "idx:NDX", "idx:RUT", "idx:DAX", "idx:FTSE", "idx:NIKKEI", "idx:STOXX50"),
    "us_equity_index": ("idx:SPX", "idx:NDX", "idx:RUT", "idx:DJI"),
}

BOOK: dict[str, Callable[[], Any]] = {
    "trend": lambda: lib.trend_forecast("compound_sigmoid"),
    "breakout": lambda: lib.breakout(60),
    "momentum": lambda: lib.time_series_momentum(252, 21),
    "reversion": lambda: lib.zscore_reversion(40),
}

REGIMES: dict[str, Callable[[], Any]] = {}

EXPLORATORY = "EXPLORATORY"
MASTER_FILE = Path("config/instruments.toml")


def project_root(start: Path | None = None) -> Path | None:
    """The nearest folder at or above `start` (the working folder) holding the instrument master."""
    here = (start or Path.cwd()).resolve()
    return next((folder for folder in (here, *here.parents) if (folder / MASTER_FILE).exists()), None)


def use_project_root(root: Path | None = None) -> Path | None:
    """Point the lab's data, ledger and pre-registration folders at the project root, unless they are set already.

    A notebook runs in its own folder (notebooks/, notebooks/templates/); without this the catalog, the experiment
    store and the ledger would be looked for there and found empty.
    """
    root = root or project_root()
    if root is not None:
        os.environ.setdefault("QUANTLAB_DATA", str(root / "quantlab_data"))
        os.environ.setdefault("QUANTLAB_PREREG_DIR", str(root / "research" / "prereg"))
    return root


def _regimes() -> dict[str, Callable[[], Any]]:
    from quantlab.regimes.models import FeatureBlendRegime, HMMRegime, PercentileRegime, VolatilityRegime

    return {"h14_regime": FeatureBlendRegime, "efficiency_ratio": lambda: PercentileRegime("er"), "slope_t": lambda: PercentileRegime("abs_slope_t"),
            "volatility": VolatilityRegime, "hmm": HMMRegime, **REGIMES}


class PriceView:
    """`px` inside a signal function: each field as a dates x instruments frame (px.close, px.open, ...)."""

    def __init__(self, panel: Panel) -> None:
        self.panel = panel

    def __getattr__(self, name: str) -> pd.DataFrame:
        fields = self.__dict__["panel"].fields
        if name in fields:
            return fields[name]
        if name == "returns":
            return fields["close"].pct_change(fill_method=None)
        raise AttributeError(f"px has {sorted(fields)} and returns, not {name!r}")

    def __getitem__(self, name: str) -> pd.DataFrame:
        return getattr(self, name)


@dataclass
class Idea:
    """A signal in the lab: its forecast (dates x instruments, on the forecast scale), the raw values, and checks."""

    name: str
    forecast: pd.DataFrame
    raw: pd.DataFrame
    source: str = ""
    causal: bool = True
    notes: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        flag = "" if self.causal else ", USES LATER DATA"
        return f"Idea({self.name!r}: {self.forecast.shape[1]} instruments, {self.forecast.notna().any(axis=1).sum()} bars{flag})"


@dataclass
class TestReport:
    """What `Lab.test` found: the backtest, both cards, and the comparison with the book's signals."""

    idea: Idea
    result: BacktestResult
    strategy_card: Any
    signal_card: Any
    versus_book: pd.DataFrame
    seconds: float
    run_id: int | None = None

    def __repr__(self) -> str:
        return (f"TestReport({self.idea.name!r}: strategy card '{self.strategy_card.verdict}', signal card '{self.signal_card.verdict if self.signal_card else '-'}', "
                f"Sharpe {metrics(self.result).get('Sharpe', float('nan')):.2f}, {self.seconds:.1f}s)")


class Lab:
    """See the module docstring."""

    def __init__(self, universe: str | Sequence[str] = "equity_index_core", start: str | None = None, end: str | None = None, *,
                 bars: Mapping[str, pd.DataFrame] | None = None, master: Any = None, catalog: Any = None, target_vol: float = 0.10,
                 cost_bps: float = 1.5, engine: EngineConfig | None = None, hypothesis: str | None = None, store: Any = None, record: bool = True,
                 show: bool = True) -> None:
        from quantlab.validation import ledger

        self.root = use_project_root() if bars is None or master is not None else project_root()
        self.hypothesis = hypothesis
        if hypothesis is not None:
            ledger.require_prereg(hypothesis)  # confirmatory work starts from a locked pre-registration
        self.start, self.end = start, end
        self.universe = universe
        self.sizer = Sizer(target_vol, span=20, cap=2.0)
        self.engine = engine or EngineConfig(band=0.02, max_position=2.0)
        self.cost_bps = cost_bps
        self.show = show
        self._record = record
        self._store = store
        self._bars, self._master, self._catalog = bars, master, catalog
        self._book: dict[str, Idea] = {}
        self._results: dict[str, BacktestResult] = {}
        self._count = 0
        self._load()

    # --- data -----------------------------------------------------------------------------------------------------

    def _load(self) -> None:
        from quantlab.validation.ledger import holdout_cut

        if self._bars is not None:
            frames = {name: holdout_cut(frame.loc[self.start:self.end]) for name, frame in self._bars.items()}
            self.panel = align(frames)
            self.returns = self.panel["close"].pct_change(fill_method=None)
            self.costs: pd.DataFrame | float = self.cost_bps
            return
        from quantlab.data.instruments import InstrumentMaster
        from quantlab.engine.returns import cost_table, excess_returns

        self._master = self._master or InstrumentMaster.load((self.root or Path.cwd()) / MASTER_FILE)
        ids = list(UNIVERSES[self.universe]) if isinstance(self.universe, str) else list(self.universe)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN windows in early, close-only history; the data cards report them
            self.panel = load_panel(self._master, ids, catalog=self._catalog, start=self.start, end=self.end)
        self.returns = excess_returns(self.panel, self._master, catalog=self._catalog)
        self.costs = cost_table(self.panel, self._master)

    @property
    def instruments(self) -> list[str]:
        return self.panel.instruments

    @property
    def store(self) -> Any:
        if self._store is None:
            from quantlab.tracking.store import ExperimentStore

            self._store = ExperimentStore()
        return self._store

    # --- signals --------------------------------------------------------------------------------------------------

    def _compute(self, source: Any, panel: Panel, per_instrument: bool) -> pd.DataFrame:
        if hasattr(source, "warmup") and callable(source):  # a library signal or FunctionSignal
            return apply_signal(source, panel)
        if per_instrument:
            return pd.DataFrame({name: source(panel.bars(name)) for name in panel.instruments}, index=panel.index)
        out = source(PriceView(panel))
        if isinstance(out, pd.Series):
            if len(panel.instruments) == 1:
                return out.to_frame(panel.instruments[0])
            raise TypeError("the function returned one series for several instruments: return a dates x instruments frame, or pass per_instrument=True")
        return out.reindex(index=panel.index, columns=panel.instruments)

    def _hindsight(self, source: Any, values: pd.DataFrame, per_instrument: bool) -> list[str]:
        """Recompute on the first 70% of the history; any change to those values means the signal saw later data."""
        cut = self.panel.index[int(len(self.panel.index) * 0.7)]
        early = self._compute(source, self.panel.window(end=cut), per_instrument)
        full = values.loc[early.index, early.columns]
        both = early.notna() & full.notna()
        differ = both & ~np.isclose(early.where(both, 0.0), full.where(both, 0.0), rtol=1e-7, atol=1e-10)
        differ |= early.isna() != full.isna()
        if not differ.to_numpy().any():
            return []
        first = differ.any(axis=1).idxmax()
        columns = list(differ.columns[differ.any()])
        return [f"later data changes earlier values (first on {first:%Y-%m-%d}, in {', '.join(map(str, columns[:5]))}): the signal uses the future. "
                "Common causes: ranking or scaling down a column over all dates (.rank(), .mean(), .std() without rolling), "
                "centred windows, .shift(-n), or fitting on the whole history."]

    def signal(self, source: Any, name: str | None = None, *, per_instrument: bool = False, normalize: bool = True, check: bool = True) -> Idea:
        """An idea from a function of `px`, a library signal, or (per_instrument=True) a function of one instrument's bars."""
        raw = self._compute(source, self.panel, per_instrument).astype(float)
        notes = self._hindsight(source, raw, per_instrument) if check else []
        for note in notes:
            warnings.warn(f"{name or 'signal'}: {note}", stacklevel=2)
        forecast = raw.apply(lambda column: lib.normalize(column)) if normalize else raw.clip(-2.0, 2.0)
        if name is None:
            given = getattr(source, "name", None) or getattr(source, "__name__", "")
            self._count += 1
            name = given if given and given != "<lambda>" else f"idea {self._count}"
        try:
            text = inspect.getsource(source).strip() if not hasattr(source, "warmup") else repr(source)
        except (OSError, TypeError):
            text = repr(source)
        return Idea(name, forecast, raw, text, causal=not notes, notes=notes)

    def book_signal(self, name: str) -> Idea:
        """One of the book's signals (BOOK), built like any idea and kept."""
        if name not in self._book:
            self._book[name] = self.signal(BOOK[name](), name=name, check=False)
        return self._book[name]

    def _as_idea(self, item: Idea | str) -> Idea:
        return item if isinstance(item, Idea) else self.book_signal(item)

    # --- trading --------------------------------------------------------------------------------------------------

    def backtest(self, item: Idea | str | Targets, *, cost_multiplier: float = 1.0, name: str | None = None) -> BacktestResult:
        """The engine's result for an idea, a book signal or ready-made targets (equal weights, `target_vol` each)."""
        targets = item if isinstance(item, Targets) else Targets(self._as_idea(item).forecast)
        config = self.engine if cost_multiplier == 1.0 else dataclasses.replace(self.engine, cost_multiplier=cost_multiplier)
        weights = 1.0 / max(len(self.instruments), 1)
        result = run_backtest(targets, self.returns, costs_bps=self.costs, sizer=self.sizer, weights=weights, config=config)
        key = name or (item.name if isinstance(item, Idea) else item if isinstance(item, str) else None)
        if key is not None and cost_multiplier == 1.0:
            self._results[key] = result
        return result

    def _result(self, item: Idea | str) -> BacktestResult:
        name = item.name if isinstance(item, Idea) else item
        return self._results.get(name) or self.backtest(item)

    def hold(self) -> BacktestResult:
        """Long every instrument at the volatility target: the baseline every idea is compared with."""
        if "__hold__" not in self._results:
            self._results["__hold__"] = self.backtest(Targets(pd.DataFrame(1.0, index=self.panel.index, columns=self.instruments).where(self.panel["close"].notna())),
                                                      name="__hold__")
        return self._results["__hold__"]

    # --- recording ------------------------------------------------------------------------------------------------

    @property
    def confirmatory(self) -> bool:
        return self.hypothesis is not None

    @contextlib.contextmanager
    def _run(self, name: str, config: Mapping[str, Any]) -> Iterator[Any]:
        if not self._record:
            yield None
            return
        hypothesis = self.hypothesis or EXPLORATORY
        if hypothesis not in self.store.hypotheses().index:
            title = "Exploratory runs in notebooks: counted, never trials" if hypothesis == EXPLORATORY else hypothesis
            self.store.register(hypothesis, title, status="running" if self.confirmatory else "idea")
        config = {"universe": self.universe if isinstance(self.universe, str) else list(self.universe), "start": self.start, "end": self.end,
                  "target_vol": self.sizer.target_vol, "mode": "confirmatory" if self.confirmatory else "exploratory", **config}
        with self.store.run(hypothesis, name, config=config, snapshots=list(self.panel.snapshots.values()),
                            notes="confirmatory" if self.confirmatory else "exploratory: not a trial") as run:
            yield run

    def exploration_count(self) -> int:
        """Exploratory runs recorded so far (the honest count of how much was looked at before pre-registering)."""
        runs = self.store.runs(EXPLORATORY)
        return int(len(runs))

    # --- the questions --------------------------------------------------------------------------------------------

    def test(self, item: Idea | str, *, horizons: Sequence[int] = (5, 20), show: bool | None = None) -> TestReport:
        """Signal card, strategy card (against holding everything), costs, and the idea next to the book's signals."""
        from quantlab.validation import ledger
        from quantlab.validation.signal_card import signal_card
        from quantlab.validation.strategy_card import card_backtest

        started = time.perf_counter()
        idea = self._as_idea(item)
        result = self.backtest(idea)
        family = self.hypothesis if self.confirmatory else None
        strategy = card_backtest(result, name=idea.name, baseline=self.hold(), family=family, ledger=self.confirmatory)
        close = self.panel["close"]
        forecast = idea.forecast.where(close.notna())
        try:
            if forecast.shape[1] >= 5:
                card = signal_card(forecast, close, horizons=horizons, name=idea.name, family=family or "", ledger=self.confirmatory)
            else:
                card = signal_card(forecast.iloc[:, 0], close.iloc[:, 0], horizons=horizons, name=idea.name, family=family or "", ledger=self.confirmatory)
        except (ValueError, KeyError) as error:  # too little data for a card is a result, not a crash
            card = None
            idea.notes.append(f"signal card not computed: {error}")
        if self.confirmatory:
            ledger.log_trial(self.hypothesis, f"lab test {idea.name}", {"idea": idea.name}, result.pnl.to_numpy())
        rows = {}
        own = result.pnl
        for name in BOOK:
            other = self._result(name).pnl
            rows[name] = {"Sharpe": metrics(self._result(name)).get("Sharpe"), "P&L correlation with the idea": float(own.corr(other)),
                          "forecast correlation": float(idea.forecast.stack().corr(self.book_signal(name).forecast.stack()))}
        doubled = metrics(self.backtest(idea, cost_multiplier=2.0)).get("Sharpe")
        mine = metrics(result)
        rows[f"{idea.name} (the idea)"] = {"Sharpe": mine.get("Sharpe"), "P&L correlation with the idea": 1.0, "forecast correlation": 1.0}
        versus = pd.DataFrame(rows).T
        versus.attrs["Sharpe at 2x costs"] = doubled
        report = TestReport(idea, result, strategy, card, versus, time.perf_counter() - started)
        with self._run(f"test {idea.name}", {"idea": idea.name, "source": idea.source[:2000], "horizons": list(horizons)}) as run:
            if run is not None:
                run.log_result(result)
                run.log_card(strategy)
                if card is not None:
                    run.log_card(card)
                report.run_id = run.id
        if self.show if show is None else show:
            self._show_test(report, mine)
        return report

    def _show_test(self, report: TestReport, mine: Mapping[str, float]) -> None:
        import matplotlib.pyplot as plt

        from quantlab.reporting import charts

        idea = report.idea
        lines = [f"{idea.name}: strategy card '{report.strategy_card.verdict}' ({report.strategy_card.reason})"]
        if report.signal_card is not None:
            lines.append(f"signal card '{report.signal_card.verdict}' ({report.signal_card.reason})")
        lines.append(f"Sharpe {mine.get('Sharpe', float('nan')):.2f} net, {mine.get('gross Sharpe', float('nan')):.2f} gross, "
                     f"{report.versus_book.attrs['Sharpe at 2x costs']:.2f} at twice the costs; turnover {mine.get('turnover', float('nan')):.1f}x a year; "
                     f"max drawdown {mine.get('max drawdown', float('nan')):.1%}")
        lines += [f"WARNING: {note}" for note in idea.notes]
        lines.append("exploratory: recorded, not a trial" if not self.confirmatory else f"confirmatory: a trial of {self.hypothesis}")
        print("\n".join(lines))
        _display(report.versus_book.round(2))
        with plt.rc_context(charts.style()):
            figure, axes = plt.subplots(1, 2, figsize=(14, 4))
            charts.equity({idea.name: report.result, **{name: self._result(name) for name in BOOK}, "hold everything": self.hold()}, ax=axes[0],
                          title="Growth of 1 (each at the same volatility target)")
            charts.attribution(report.result, by="instrument", ax=axes[1])
            figure.tight_layout()
        plt.show()

    def combine(self, *items: Idea | str, weights: Mapping[str, float] | None = None, overlay: str | Any = None,
                state_weights: Mapping[str, Mapping[str, float]] | None = None, show: bool | None = None) -> dict[str, Any]:
        """A book of several ideas: a static blend, or switched by a regime model (`overlay`), against its benchmarks.

        overlay="h14_regime" with two ideas, one of them "trend", uses H14's rule unless `state_weights` is given:
        trend in TREND, the other in RANGE, half of each in TRANSITION.
        """
        ideas = [self._as_idea(item) for item in items]
        names = [idea.name for idea in ideas]
        forecasts = {idea.name: idea.forecast.fillna(0.0) for idea in ideas}
        static = dict(weights or {name: 1.0 / len(names) for name in names})
        books: dict[str, Targets] = {}
        if overlay is None:
            books["blend"] = Targets(sum(forecasts[name] * static[name] for name in names).clip(-2, 2), {name: forecasts[name] * static[name] for name in names})
        else:
            model = _regimes()[overlay]() if isinstance(overlay, str) else overlay
            if state_weights is None:
                if overlay != "h14_regime" or len(names) != 2:
                    raise ValueError("say which idea each state uses: state_weights={state: {idea: weight}}")
                trend = "trend" if "trend" in names else names[1]
                other = next(name for name in names if name != trend)
                state_weights = {"TREND": {trend: 1.0}, "RANGE": {other: 1.0}, "TRANSITION": {trend: 0.5, other: 0.5}}
            probabilities = {instrument: model(self.panel.bars(instrument)) for instrument in self.instruments}
            books["overlay"] = self._overlay(forecasts, probabilities, state_weights, hard=False)
            books["hard switch"] = self._overlay(forecasts, probabilities, state_weights, hard=True)
            average = {name: float(np.mean([state_weights[state].get(name, 0.0) for state in state_weights])) for name in names}
            books["static blend"] = Targets(sum(forecasts[name] * average[name] for name in names).clip(-2, 2))
            books["exposure-matched static"] = Targets(books["overlay"].target.abs() * np.sign(books["static blend"].target))
        for name in names:
            books[f"{name} alone"] = Targets(forecasts[name])
        results = {name: self.backtest(targets, name=f"combine:{name}") for name, targets in books.items()}
        table = pd.DataFrame({name: metrics(result) for name, result in results.items()}).T[["Sharpe", "annual return", "volatility", "max drawdown", "turnover"]]
        with self._run(f"combine {' + '.join(names)}" + (f" by {overlay if isinstance(overlay, str) else type(overlay).__name__}" if overlay is not None else ""),
                       {"ideas": names, "overlay": str(overlay), "state_weights": state_weights, "weights": static}) as run:
            if run is not None:
                for name, result in results.items():
                    run.log_metrics({f"{name}: {key}": value for key, value in metrics(result).items() if key in table.columns})
        if self.show if show is None else show:
            import matplotlib.pyplot as plt

            from quantlab.reporting import charts

            _display(table.round(3))
            charts.equity(results, title="Growth of 1: the combination and its benchmarks")
            plt.show()
        return {"table": table, "results": results, "targets": books}

    def _overlay(self, forecasts: Mapping[str, pd.DataFrame], probabilities: Mapping[str, pd.DataFrame], state_weights: Mapping[str, Mapping[str, float]],
                 *, hard: bool) -> Targets:
        from quantlab.regimes.models import labels, one_hot

        parts = {name: pd.DataFrame(0.0, index=self.panel.index, columns=self.instruments) for name in forecasts}
        for instrument, probs in probabilities.items():
            probs = one_hot(labels(probs), tuple(probs.columns)) if hard else probs
            known = probs.notna().all(axis=1)
            for name in forecasts:
                weight = sum(probs[state].fillna(0.0) * float(state_weights.get(state, {}).get(name, 0.0)) for state in probs.columns)
                parts[name][instrument] = (forecasts[name][instrument] * weight).where(known, 0.0)
        return Targets(sum(parts.values()).clip(-2, 2), parts)

    def compare_with_book(self, item: Idea | str, *, book: Mapping[str, Any] | pd.Series | None = None, show: bool | None = None) -> dict[str, Any]:
        """The idea against and inside the book (BOOK's signals at equal risk, or the P&L or results given).

        The idea joins at the risk of one book member. Marginal Sharpe and drawdown are the change from adding it;
        the diversification ratio is the weighted volatilities over the combined volatility (1 = no diversification);
        the best weight is the two-asset tangency weight of the idea against the book, as a share of the combined risk.
        """
        idea = self._as_idea(item)
        own = self._result(idea).pnl
        if book is None:
            members = {name: self._result(name).pnl for name in BOOK if name != idea.name}
        elif isinstance(book, pd.Series):
            members = {"book": book}
        else:
            members = {name: value.pnl if hasattr(value, "pnl") else value for name, value in book.items()}
        frame = pd.DataFrame(members).reindex(own.index).fillna(0.0)
        equal = frame.div(frame.std()).mean(axis=1) * frame.std().mean()  # members at equal risk, scaled back to a member's risk
        weight = equal.std() / own.std() / max(len(members), 1) if own.std() > 0 else 0.0
        combined = equal + weight * own

        def sharpe(series: pd.Series) -> float:
            return float(series.mean() / series.std() * math.sqrt(252.0)) if series.std() > 0 else float("nan")

        def drawdown(series: pd.Series) -> float:
            growth = (1.0 + series).cumprod()
            return float((growth / growth.cummax() - 1.0).min())

        rho = float(own.corr(equal))
        s_book, s_idea = sharpe(equal), sharpe(own)
        tangency = (s_idea - rho * s_book) / ((s_book - rho * s_idea) + (s_idea - rho * s_book)) if (s_book + s_idea) * (1 - rho) != 0 else float("nan")
        diversification = float((equal.std() + weight * own.std()) / combined.std()) if combined.std() > 0 else float("nan")
        table = pd.Series({"correlation with the book": rho, "book Sharpe": s_book, "idea Sharpe": s_idea, "book + idea Sharpe": sharpe(combined),
                           "marginal Sharpe": sharpe(combined) - s_book, "book max drawdown": drawdown(equal), "book + idea max drawdown": drawdown(combined),
                           "marginal drawdown": drawdown(combined) - drawdown(equal), "diversification ratio": diversification,
                           "best risk share for the idea (tangency)": float(np.clip(tangency, -1.0, 2.0)) if np.isfinite(tangency) else float("nan")}, name=idea.name)
        correlations = pd.concat({idea.name: own, **members}, axis=1).corr().loc[idea.name].drop(idea.name)
        with self._run(f"compare {idea.name} with the book", {"idea": idea.name, "book": list(members)}) as run:
            if run is not None:
                run.log_metrics(table.to_dict())
        if self.show if show is None else show:
            import matplotlib.pyplot as plt

            from quantlab.reporting import charts

            _display(table.round(3).to_frame())
            print("correlation with each book member:", ", ".join(f"{name} {value:+.2f}" for name, value in correlations.items()))
            charts.equity({"book + idea": combined, "book": equal, idea.name: own}, title="The book with and without the idea")
            plt.show()
        return {"table": table, "correlations": correlations, "combined": combined, "book": equal}

    def report(self, item: Idea | str, *, definition: str = "", path: str | None = None, labels: pd.Series | None = None) -> Any:
        """The standard report of an idea (quantlab.reporting.report), logged with the run."""
        from quantlab.reporting.report import build_report
        from quantlab.validation.strategy_card import card_backtest

        idea = self._as_idea(item)
        result = self._result(idea)
        card = card_backtest(result, name=idea.name, baseline=self.hold(), family=self.hypothesis, ledger=self.confirmatory)
        with self._run(f"report {idea.name}", {"idea": idea.name}) as run:
            out = build_report(result, name=idea.name, path=path, definition=definition or idea.source, card=card, baseline=self.hold(),
                               data_cards=list(self.panel.cards.values()), labels=labels, cost_run=lambda m: self.backtest(idea, cost_multiplier=m),
                               hypothesis=self.hypothesis or "exploratory", snapshots=list(self.panel.snapshots.values()))
            if run is not None:
                run.log_artefact("report", out)
        return out

    @contextlib.contextmanager
    def final_look(self, *, approved_by: str, again: str | None = None) -> Iterator["Lab"]:
        """Confirmatory only: reload the data with the frozen holdout open, once per hypothesis (logged)."""
        from quantlab.validation import ledger

        if not self.confirmatory:
            raise RuntimeError("the frozen holdout is for confirmatory tests: create the lab with hypothesis=...")
        with ledger.holdout_look(f"{self.hypothesis} final", approved_by=approved_by, again=again):
            catalog = self._catalog
            if catalog is None:
                from quantlab.data.catalog import DataCatalog

                catalog = self._catalog = DataCatalog()
            if hasattr(catalog, "unlock"):
                catalog.unlock(f"{self.hypothesis} final look, approved by {approved_by}")
            saved = (self.panel, self.returns, self.costs, dict(self._results), dict(self._book))
            self._results, self._book = {}, {}
            try:
                self._load()
                yield self
            finally:
                if hasattr(catalog, "lock"):
                    catalog.lock()
                self.panel, self.returns, self.costs, self._results, self._book = saved


def _display(item: Any) -> None:
    try:
        from IPython.display import display

        display(item)
    except ImportError:
        print(item)
