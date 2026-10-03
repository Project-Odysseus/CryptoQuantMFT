"""Research governance: a frozen final holdout, a global trial ledger, and reproducible run manifests.

Three habits that keep conclusions honest as the number of studies grows:

1. **A frozen final holdout.** Every study's own holdout (the last 30%) has been looked at many times, and each
   look that kept or dropped an idea turned it a little more into in-sample data. So the most recent stretch of
   every data set is frozen: research loaders cut it off, and only a final test of a finished candidate may look.
   Long series (starting before 2025) are frozen from `FINAL_HOLDOUT_START`; short, recent series (3-minute
   candles, recordings) keep their own last 25% frozen instead, so they stay usable. Unlocking is explicit
   (`final_holdout(reason)` or `CQM_FINAL_HOLDOUT=<reason>`) and every look is written to the ledger.
2. **One global trial count.** The deflated Sharpe ratio is only honest if `trials` counts every configuration
   ever tried on the data. Every sweep appends what it tried to an append-only ledger (`record_trials`), and
   `total_trials()` reads the total.
3. **Reproducible runs.** `write_manifest` stores the git commit (and whether the tree had uncommitted changes),
   a fingerprint of the data, and the arguments next to a study's outputs.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

FINAL_HOLDOUT_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
SHORT_SERIES_START = datetime(2025, 1, 1, tzinfo=timezone.utc)  # series starting after this keep their own frozen tail
SHORT_SERIES_TAIL = 0.25
DEFAULT_LEDGER = Path("data/research/trial_ledger.jsonl")
# Configurations tried before the ledger existed (2026-09-18 to 09-27), estimated from docs/research_log.md:
# the 10-strategy sweeps on spot and perps (~480), intraday, positioning, volatility, carry and cross-sectional
# studies (~180), portfolio, risk and stop studies (~40), and the EMA x VWAP study (~480).
BACKFILL_TRIALS = 1_180

_unlocked_reason: str | None = None
_logged_looks: set[str] = set()


# --- the frozen final holdout ---------------------------------------------------------------------------------------

def _utc(value: Any) -> datetime:
    stamp = pd.Timestamp(value)
    return (stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")).to_pydatetime()


def research_cutoff(first: Any, last: Any) -> datetime:
    """Where research data ends for a series spanning `first` to `last`: data at or after it is frozen."""
    first, last = _utc(first), _utc(last)
    if first < SHORT_SERIES_START:
        return FINAL_HOLDOUT_START
    tail = first + (last - first) * (1.0 - SHORT_SERIES_TAIL)
    # A series that began before the final holdout (e.g. a coin listed in 2025) must never reach past it: its own
    # tail rule is only for series that begin inside the holdout (recordings), which would otherwise be all frozen.
    return min(tail, FINAL_HOLDOUT_START) if first < FINAL_HOLDOUT_START else tail


def unlocked_reason() -> str | None:
    """Why the final holdout is open right now (a `final_holdout` block or `CQM_FINAL_HOLDOUT`), else None."""
    return _unlocked_reason or os.environ.get("CQM_FINAL_HOLDOUT") or None


@contextlib.contextmanager
def final_holdout(reason: str) -> Iterator[None]:
    """Open the frozen data for a final test of a finished candidate. Logged; use once per candidate, then decide."""
    global _unlocked_reason
    if not reason.strip():
        raise ValueError("say which candidate the final holdout is opened for")
    previous, _unlocked_reason = _unlocked_reason, reason
    try:
        yield
    finally:
        _unlocked_reason = previous


def _note_look(what: str) -> None:
    reason = unlocked_reason()
    key = f"{reason}|{what}"
    if reason and key not in _logged_looks:
        _logged_looks.add(key)
        _append({"type": "final_holdout_look", "reason": reason, "data": what})


def trim_bars(bars: Sequence[Any], *, label: str = "") -> list[Any]:
    """`bars` without the frozen final holdout (unchanged while it is unlocked, which is logged)."""
    bars = list(bars)
    if not bars:
        return bars
    if unlocked_reason():
        _note_look(label or getattr(bars[0], "symbol", "bars"))
        return bars
    cutoff = research_cutoff(bars[0].timestamp, bars[-1].timestamp)
    return [bar for bar in bars if _utc(bar.timestamp) < cutoff]


def trim_frame(frame: pd.DataFrame, *, column: str | None = None, label: str = "") -> pd.DataFrame:
    """A time-indexed frame (or one with a time `column`) without the frozen final holdout."""
    if frame.empty:
        return frame
    times = pd.to_datetime(frame[column] if column else frame.index, utc=True)
    if unlocked_reason():
        _note_look(label or "frame")
        return frame
    cutoff = pd.Timestamp(research_cutoff(times.min(), times.max()))
    return frame[pd.Series(times < cutoff, index=frame.index).to_numpy()]


# --- the trial ledger -------------------------------------------------------------------------------------------------

def ledger_path() -> Path:
    """The append-only ledger (`CQM_TRIAL_LEDGER` overrides the default, e.g. in tests)."""
    return Path(os.environ.get("CQM_TRIAL_LEDGER") or DEFAULT_LEDGER)


def _append(entry: dict[str, Any]) -> None:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "commit": git_commit(), **entry}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def record_trials(study: str, configurations: int, *, family: str = "", data: str = "", details: Mapping[str, Any] | None = None) -> None:
    """Add `configurations` tried by `study` to the ledger (one per parameter set x side x cost case, not per symbol)."""
    if configurations > 0:
        _append({"type": "trials", "study": study, "family": family, "configurations": int(configurations), "data": data, "details": dict(details or {})})


def ledger_entries() -> list[dict[str, Any]]:
    """Every ledger entry, oldest first."""
    path = ledger_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def total_trials(*, family: str | None = None, include_backfill: bool = True) -> int:
    """Configurations tried so far (optionally one family), for `deflated_sharpe_ratio(trials=...)`."""
    entries = [entry for entry in ledger_entries() if entry.get("type") == "trials" and (family is None or entry.get("family") == family)]
    return sum(int(entry["configurations"]) for entry in entries) + (BACKFILL_TRIALS if include_backfill and family is None else 0)


# --- pre-registrations ------------------------------------------------------------------------------------------------

PREREG_DIR = Path("research/prereg")
PLACEHOLDER = "TODO"


class PreregNotLocked(RuntimeError):
    """A study was started before its pre-registration was written and locked, or the text changed after the lock."""


def prereg_path(hypothesis: str) -> Path:
    """Where a hypothesis's pre-registration lives (`CQM_PREREG_DIR` overrides the folder, e.g. in tests)."""
    return Path(os.environ.get("CQM_PREREG_DIR") or PREREG_DIR) / f"{hypothesis}.txt"


def _prereg_hash(hypothesis: str) -> str:
    return hashlib.sha256(prereg_path(hypothesis).read_bytes()).hexdigest()


def prereg_locks(hypothesis: str) -> list[dict[str, Any]]:
    """Every lock of this hypothesis in the ledger, oldest first (one per version)."""
    return [entry for entry in ledger_entries() if entry.get("type") == "prereg" and entry.get("hypothesis") == hypothesis]


def lock_prereg(hypothesis: str) -> dict[str, Any]:
    """Record the pre-registration's text (its SHA-256) in the ledger, so results can't quietly reshape it afterwards.

    Locking the same text again changes nothing. Locking a changed text is a new version: allowed, visible in the
    ledger, and a reason to count the earlier runs as extra trials. A file that still has unfilled `TODO` fields
    is refused.
    """
    path = prereg_path(hypothesis)
    if not path.exists():
        raise FileNotFoundError(f"no pre-registration at {path}; create it with scripts/research/new_hypothesis.py new {hypothesis} \"title\"")
    if PLACEHOLDER in path.read_text(encoding="utf-8"):
        raise PreregNotLocked(f"{path} still has {PLACEHOLDER} fields: fill every section in before locking")
    digest, locks = _prereg_hash(hypothesis), prereg_locks(hypothesis)
    if locks and locks[-1]["sha256"] == digest:
        return locks[-1]
    _append({"type": "prereg", "hypothesis": hypothesis, "sha256": digest, "version": len(locks) + 1, "path": str(path)})
    return prereg_locks(hypothesis)[-1]


def prereg_status(hypothesis: str) -> str:
    """missing | unlocked | locked | changed (the text differs from its latest lock)."""
    if not prereg_path(hypothesis).exists():
        return "missing"
    locks = prereg_locks(hypothesis)
    if not locks:
        return "unlocked"
    return "locked" if locks[-1]["sha256"] == _prereg_hash(hypothesis) else "changed"


def require_prereg(hypothesis: str) -> dict[str, Any]:
    """The hypothesis's current lock; raises `PreregNotLocked` unless the text on disk is the locked one.

    Call it first in a study script: the rules are then on record before the first result exists.
    """
    status = prereg_status(hypothesis)
    if status != "locked":
        fix = {"missing": f"write it: python scripts/research/new_hypothesis.py new {hypothesis} \"title\"",
               "unlocked": f"lock it: python scripts/research/new_hypothesis.py lock {hypothesis}",
               "changed": f"the text changed after its lock; bump its version line and lock it again: python scripts/research/new_hypothesis.py lock {hypothesis}"}[status]
        raise PreregNotLocked(f"pre-registration {hypothesis} is {status}. {fix}")
    return prereg_locks(hypothesis)[-1]


# --- reproducibility ------------------------------------------------------------------------------------------------

def git_commit() -> str:
    """The current commit, with "+dirty" when the working tree has uncommitted changes to tracked files."""
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{commit}+dirty" if dirty else commit


def data_fingerprint(data: Mapping[str, Sequence[Any]] | pd.DataFrame) -> str:
    """A short hash of the data a run used: per series its length, first and last time, and the sum of closes."""
    digest = hashlib.sha256()
    if isinstance(data, pd.DataFrame):
        digest.update(pd.util.hash_pandas_object(data, index=True).values.tobytes())
    else:
        for name in sorted(data):
            bars = list(data[name])
            summary = (name, len(bars), str(bars[0].timestamp) if bars else "", str(bars[-1].timestamp) if bars else "", round(sum(bar.close for bar in bars), 6))
            digest.update(repr(summary).encode())
    return digest.hexdigest()[:16]


def write_manifest(out_dir: Path | str, *, args: Mapping[str, Any] | Any = None, data: str = "", extra: Mapping[str, Any] | None = None) -> Path:
    """Write `manifest.json` next to a study's outputs: commit, data fingerprint, arguments, holdout state."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    arguments = vars(args) if hasattr(args, "__dict__") else dict(args or {})
    manifest = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "commit": git_commit(), "data": data,
                "args": {key: str(value) for key, value in arguments.items()}, "final_holdout_start": FINAL_HOLDOUT_START.isoformat(),
                "final_holdout_unlocked": unlocked_reason(), **dict(extra or {})}
    path = out / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return path


def frozen_window_note(first: Any, last: Any) -> str:
    """A one-line description of what is frozen for a series, for study printouts."""
    cutoff = research_cutoff(first, last)
    return f"final holdout frozen from {cutoff:%Y-%m-%d}" + (" (unlocked: " + str(unlocked_reason()) + ")" if unlocked_reason() else "")
