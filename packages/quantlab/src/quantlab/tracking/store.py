"""The experiment store and the hypothesis registry: the index of everything the lab has run (plan 5.1-5.2).

One SQLite database, `$QUANTLAB_DATA/research/lab.db` (default `quantlab_data/research/lab.db`), separate from any
host database. The SQL is plain (no SQLite-only types or functions), so the same schema runs on Postgres with a
different `connect`. The ledger (quantlab.validation.ledger) stays the append-only record of trials and looks; this
store is the queryable index of runs, their configs, data, metrics, cards and artefacts.

    store = ExperimentStore()
    store.register("H15", "Bond trend with a carry tilt", status="pre-registered")
    with store.run("H15", "primary", config=cfg, snapshots=[snap.id], hypothesis_version=1) as run:
        result = run_backtest(...)
        run.log_result(result)                  # metrics and the P&L series
        run.log_card(card_backtest(result, name="H15 primary"))
        run.log_artefact("report", build_report(...))
    store.runs("H15"); store.compare([1, 2]); store.lab_state()
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import numpy as np
import pandas as pd

from quantlab.validation.ledger import git_commit

STATUSES = ("idea", "pre-registered", "running", "passed", "inconclusive", "failed", "paper", "live", "retired")
DISPLAY_ORDER = ("live", "paper", "running", "passed", "pre-registered", "idea", "inconclusive", "failed", "retired")  # active work first

SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, decision TEXT, notes TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS status_history (
    hypothesis TEXT NOT NULL, at TEXT NOT NULL, status TEXT NOT NULL, reason TEXT);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, hypothesis TEXT, hypothesis_version INTEGER, name TEXT NOT NULL, created TEXT NOT NULL, finished TEXT,
    state TEXT NOT NULL, config TEXT, config_hash TEXT, snapshots TEXT, git_commit TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS metrics (
    run INTEGER NOT NULL, key TEXT NOT NULL, value REAL, PRIMARY KEY (run, key));
CREATE TABLE IF NOT EXISTS cards (
    run INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, verdict TEXT NOT NULL, p REAL, reason TEXT, fingerprint TEXT);
CREATE TABLE IF NOT EXISTS artefacts (
    run INTEGER NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL);
"""


def default_path() -> Path:
    return Path(os.environ.get("QUANTLAB_DATA", "quantlab_data")) / "research" / "lab.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Run:
    """One run in progress: log what it produced; the store records it as it goes."""

    store: "ExperimentStore"
    id: int
    folder: Path

    def log_metrics(self, values: Mapping[str, Any]) -> None:
        rows = [(self.id, str(key), float(value)) for key, value in values.items() if isinstance(value, (int, float, np.integer, np.floating))]
        with self.store._connect() as db:
            db.executemany("DELETE FROM metrics WHERE run = ? AND key = ?", [(run, key) for run, key, _ in rows])
            db.executemany("INSERT INTO metrics (run, key, value) VALUES (?, ?, ?)", rows)

    def log_series(self, name: str, series: pd.Series | pd.DataFrame) -> Path:
        """Save a series as CSV in the run's folder and register it as an artefact."""
        path = self.folder / f"{name}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        series.to_csv(path)
        self.log_artefact(f"series:{name}", path)
        return path

    def log_result(self, result: Any, *, prefix: str = "") -> None:
        """An engine result's metrics (quantlab.analytics.performance.metrics) and its net P&L series."""
        from quantlab.analytics.performance import metrics

        self.log_metrics({f"{prefix}{key}": value for key, value in metrics(result).items()})
        self.log_series(f"{prefix}pnl", result.pnl.rename("pnl"))

    def log_card(self, card: Any) -> None:
        """A strategy, signal or regime card's verdict (and its headline numbers as metrics)."""
        kind = "strategy" if hasattr(card, "versus_baseline") else "signal" if hasattr(card, "horizons") else "regime"
        numbers = card.details.get("numbers", {}) if kind == "strategy" else {}
        p_value = card.details.get("placebo_p") if kind == "regime" else numbers.get("placebo_p")
        with self.store._connect() as db:
            db.execute("INSERT INTO cards (run, kind, name, verdict, p, reason, fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (self.id, kind, card.name, card.verdict, None if p_value is None else float(p_value), card.reason, getattr(card, "fingerprint", "")))
        self.log_metrics({f"card {key}": value for key, value in numbers.items()})

    def log_artefact(self, kind: str, path: str | Path) -> None:
        with self.store._connect() as db:
            db.execute("INSERT INTO artefacts (run, kind, path) VALUES (?, ?, ?)", (self.id, kind, str(path)))

    def log_figure(self, name: str, figure: Any) -> Path:
        path = self.folder / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(path, dpi=120, bbox_inches="tight")
        self.log_artefact(f"figure:{name}", path)
        return path


class ExperimentStore:
    """Runs and hypotheses in one database (see the module docstring)."""

    def __init__(self, path: str | Path | None = None, *, connect: Callable[[], Any] | None = None) -> None:
        self.path = Path(path) if path else default_path()
        self._connect_fn = connect
        with self._connect() as db:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[Any]:
        if self._connect_fn is not None:
            db = self._connect_fn()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _query(self, sql: str, params: Sequence[Any] = ()) -> pd.DataFrame:
        with self._connect() as db:
            cursor = db.execute(sql, tuple(params))
            columns = [item[0] for item in cursor.description]
            return pd.DataFrame(cursor.fetchall(), columns=columns)

    # --- hypotheses -------------------------------------------------------------------------------------------------

    def register(self, hypothesis: str, title: str, *, status: str = "idea", notes: str = "") -> None:
        """Add a hypothesis (or update its title and notes if it exists; its status changes only via set_status)."""
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        now = _now()
        with self._connect() as db:
            exists = db.execute("SELECT 1 FROM hypotheses WHERE id = ?", (hypothesis,)).fetchone()
            if exists:
                db.execute("UPDATE hypotheses SET title = ?, notes = ?, updated = ? WHERE id = ?", (title, notes, now, hypothesis))
                return
            db.execute("INSERT INTO hypotheses (id, title, status, decision, notes, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (hypothesis, title, status, None, notes, now, now))
            db.execute("INSERT INTO status_history (hypothesis, at, status, reason) VALUES (?, ?, ?, ?)", (hypothesis, now, status, "registered"))

    def set_status(self, hypothesis: str, status: str, *, reason: str, decision: str | None = None) -> None:
        """Move a hypothesis to `status`, with the reason (kept in its history) and optionally the decision text."""
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        if not reason.strip():
            raise ValueError("say why the status changes")
        now = _now()
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM hypotheses WHERE id = ?", (hypothesis,)).fetchone():
                raise KeyError(f"unknown hypothesis {hypothesis!r}; register it first")
            if decision is not None:
                db.execute("UPDATE hypotheses SET status = ?, decision = ?, updated = ? WHERE id = ?", (status, decision, now, hypothesis))
            else:
                db.execute("UPDATE hypotheses SET status = ?, updated = ? WHERE id = ?", (status, now, hypothesis))
            db.execute("INSERT INTO status_history (hypothesis, at, status, reason) VALUES (?, ?, ?, ?)", (hypothesis, now, status, reason))

    def hypotheses(self) -> pd.DataFrame:
        return self._query("SELECT id, title, status, decision, notes, created, updated FROM hypotheses").set_index("id")

    def history(self, hypothesis: str) -> pd.DataFrame:
        return self._query("SELECT at, status, reason FROM status_history WHERE hypothesis = ? ORDER BY at", (hypothesis,))

    # --- runs ---------------------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def run(self, hypothesis: str | None, name: str, *, config: Any = None, snapshots: Sequence[str] = (), hypothesis_version: int | None = None,
            notes: str = "") -> Iterator[Run]:
        """Record a run: started now, finished (or failed, with the error) when the block ends."""
        config_text = json.dumps(_plain(config), sort_keys=True, default=str) if config is not None else None
        digest = hashlib.sha256(config_text.encode()).hexdigest()[:16] if config_text is not None else None
        with self._connect() as db:
            cursor = db.execute("INSERT INTO runs (hypothesis, hypothesis_version, name, created, state, config, config_hash, snapshots, git_commit, notes) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (hypothesis, hypothesis_version, name, _now(), "running", config_text, digest, json.dumps(list(snapshots)), git_commit(), notes))
            run_id = int(cursor.lastrowid)
        run = Run(self, run_id, self.path.parent / "runs" / f"{run_id:05d}")
        try:
            yield run
        except BaseException as error:
            self._finish(run_id, f"failed: {type(error).__name__}: {error}"[:500])
            raise
        self._finish(run_id, "finished")

    def _finish(self, run_id: int, state: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE runs SET state = ?, finished = ? WHERE id = ?", (state, _now(), run_id))

    def runs(self, hypothesis: str | None = None, *, metrics: Sequence[str] = ("Sharpe", "annual return", "max drawdown")) -> pd.DataFrame:
        """Every run (of one hypothesis) with its chosen metrics and its latest card verdict."""
        where, params = ("WHERE hypothesis = ?", (hypothesis,)) if hypothesis else ("", ())
        table = self._query(f"SELECT id, hypothesis, hypothesis_version, name, created, state, config_hash, git_commit FROM runs {where} ORDER BY id", params).set_index("id")
        if table.empty:
            return table
        values = self.metrics(list(table.index))
        for key in metrics:
            table[key] = values[key] if key in values.columns else np.nan
        cards = self._query("SELECT run, verdict FROM cards ORDER BY rowid").groupby("run")["verdict"].last()
        table["verdict"] = cards.reindex(table.index)
        return table

    def metrics(self, run_ids: Sequence[int]) -> pd.DataFrame:
        """Runs x metric keys."""
        if not run_ids:
            return pd.DataFrame()
        marks = ", ".join("?" for _ in run_ids)
        rows = self._query(f"SELECT run, key, value FROM metrics WHERE run IN ({marks})", list(run_ids))
        return rows.pivot(index="run", columns="key", values="value") if len(rows) else pd.DataFrame(index=list(run_ids))

    def get(self, run_id: int) -> dict[str, Any]:
        """Everything about one run."""
        row = self._query("SELECT * FROM runs WHERE id = ?", (run_id,))
        if row.empty:
            raise KeyError(run_id)
        out = row.iloc[0].to_dict()
        out["config"] = json.loads(out["config"]) if out.get("config") else None
        out["snapshots"] = json.loads(out["snapshots"]) if out.get("snapshots") else []
        out["metrics"] = self.metrics([run_id]).iloc[0].dropna().to_dict() if run_id in self.metrics([run_id]).index else {}
        out["cards"] = self._query("SELECT kind, name, verdict, p, reason FROM cards WHERE run = ?", (run_id,))
        out["artefacts"] = self._query("SELECT kind, path FROM artefacts WHERE run = ?", (run_id,))
        return out

    def series(self, run_id: int, name: str = "pnl") -> pd.Series:
        """A series a run saved with log_series."""
        found = self._query("SELECT path FROM artefacts WHERE run = ? AND kind = ?", (run_id, f"series:{name}"))
        if found.empty:
            raise KeyError(f"run {run_id} has no series {name!r}")
        frame = pd.read_csv(found.iloc[-1]["path"], index_col=0, parse_dates=True)
        return frame.iloc[:, 0] if frame.shape[1] == 1 else frame

    def compare(self, run_ids: Sequence[int], keys: Sequence[str] | None = None) -> pd.DataFrame:
        """Metrics side by side (keys x runs), with the config differences."""
        table = self.metrics(run_ids).T
        if keys is not None:
            table = table.reindex(list(keys))
        configs = {run_id: self.get(run_id)["config"] or {} for run_id in run_ids}
        flat = {run_id: _flatten(config) for run_id, config in configs.items()}
        differing = sorted({key for values in flat.values() for key in values if len({json.dumps(v.get(key), default=str) for v in flat.values()}) > 1})
        for key in differing:
            table.loc[f"config: {key}"] = [flat[run_id].get(key) for run_id in run_ids]
        table.columns = [f"run {run_id}" for run_id in run_ids]
        return table

    def lab_state(self) -> pd.DataFrame:
        """One row per hypothesis (active work first): status, decision, runs, the latest run and its verdict."""
        hypotheses = self.hypotheses()
        runs = self._query("SELECT id, hypothesis, name, created FROM runs ORDER BY id")
        cards = self._query("SELECT run, verdict FROM cards ORDER BY rowid").groupby("run")["verdict"].last()
        rows = {}
        for hypothesis, row in hypotheses.iterrows():
            mine = runs[runs["hypothesis"] == hypothesis]
            latest = mine.iloc[-1] if len(mine) else None
            rows[hypothesis] = {"title": row["title"], "status": row["status"], "runs": len(mine),
                                "latest run": f"{latest['id']}: {latest['name']}" if latest is not None else "",
                                "latest verdict": cards.get(latest["id"], "") if latest is not None else "", "decision": row["decision"] or "",
                                "updated": row["updated"]}
        table = pd.DataFrame(rows).T.reindex(columns=["title", "status", "runs", "latest run", "latest verdict", "decision", "updated"])
        if len(table):
            order = {status: index for index, status in enumerate(DISPLAY_ORDER)}
            table = table.iloc[sorted(range(len(table)), key=lambda i: (order[table.iloc[i]["status"]], _natural(table.index[i])))]
        return table


def _natural(name: str) -> tuple[Any, ...]:
    import re

    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name))


def _plain(config: Any) -> Any:
    from dataclasses import asdict, is_dataclass

    if is_dataclass(config) and not isinstance(config, type):
        return asdict(config)
    return config


def _flatten(config: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(config, Mapping):
        out: dict[str, Any] = {}
        for key, value in config.items():
            out.update(_flatten(value, f"{prefix}{key}."))
        return out
    return {prefix.rstrip("."): config}
