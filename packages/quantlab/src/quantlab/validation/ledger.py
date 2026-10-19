"""The lab's research record: trials, cards, pre-registrations and looks at the frozen holdout (plan 4.4, 4.7).

One append-only JSON-lines file, separate from any host project's ledger:
`$QUANTLAB_LEDGER`, else `$QUANTLAB_DATA/research/ledger.jsonl` (default `quantlab_data/research/ledger.jsonl`).

- **Trials.** Every configuration a study runs is a trial (`record_trials`, `log_trial`). The deflated Sharpe and
  the lab-wide false-discovery control count them, so an idea picked from a sweep pays for the sweep.
- **Cards.** Each signal, strategy or regime card notes its verdict and p-value (`note_card`), once per identical
  input. `lab_summary` and `quantlab.validation.fdr.lab_fdr` read them back.
- **Pre-registrations.** `lock_prereg(hypothesis)` records the SHA-256 of the text in `$QUANTLAB_PREREG_DIR`
  (default `research/prereg`); `require_prereg` refuses to run a study whose text is missing, unlocked or changed.
- **The frozen holdout.** Returns from `FROZEN_FROM` on are cut from every card (`holdout_cut`). A finished candidate
  gets one look: `with holdout_look("H15 final", approved_by="owner"):` logs the look and opens the data inside the
  block. A second look at the same candidate is refused unless `again="why"` is given, and that is logged too.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

import numpy as np
import pandas as pd

from quantlab.data.catalog import DEFAULT_FROZEN_FROM

FROZEN_FROM = pd.Timestamp(DEFAULT_FROZEN_FROM)
PLACEHOLDER = "TODO"

_open_look: str | None = None


def ledger_path() -> Path:
    """Where the ledger lives (see the module docstring)."""
    explicit = os.environ.get("QUANTLAB_LEDGER")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("QUANTLAB_DATA", "quantlab_data")) / "research" / "ledger.jsonl"


def git_commit() -> str:
    """The current commit, with "+dirty" when tracked files have uncommitted changes ("unknown" outside git)."""
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{commit}+dirty" if dirty else commit


def append(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Add one entry (stamped with the time and the commit)."""
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "commit": git_commit(), **dict(entry)}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")
    return record


def entries(kind: str | None = None) -> list[dict[str, Any]]:
    """Every entry (of one `type`), oldest first."""
    path = ledger_path()
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if kind is None or row.get("type") == kind]


# --- trials ---------------------------------------------------------------------------------------------------------

def record_trials(study: str, configurations: int, *, family: str = "", data: str = "", details: Mapping[str, Any] | None = None) -> None:
    """Add `configurations` tried by `study` (one per parameter set x side x cost case, not per instrument)."""
    if configurations > 0:
        append({"type": "trials", "study": study, "family": family, "configurations": int(configurations), "data": data, "details": dict(details or {})})


def log_trial(family: str, study: str, params: Mapping[str, Any], returns: pd.Series | np.ndarray, *, data: str = "") -> None:
    """One configuration with its per-period Sharpe, so the family's Sharpe variance is known to the deflated Sharpe."""
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    spread = values.std(ddof=1) if len(values) > 1 else 0.0
    record_trials(study, 1, family=family, data=data, details={"params": dict(params), "sr_per_period": float(values.mean() / spread) if spread > 0 else None})


def total_trials(family: str | None = None) -> int:
    """Configurations tried so far (in one family)."""
    return sum(int(row["configurations"]) for row in entries("trials") if family is None or row.get("family") == family)


def family_trials(family: str) -> tuple[int, float]:
    """(configurations tried under `family`, variance of their per-period Sharpes where logged)."""
    rows = [row for row in entries("trials") if row.get("family") == family]
    sharpes = np.array([row["details"]["sr_per_period"] for row in rows if (row.get("details") or {}).get("sr_per_period") is not None], dtype=float)
    return sum(int(row["configurations"]) for row in rows), float(np.var(sharpes, ddof=1)) if len(sharpes) > 1 else 0.0


# --- cards ----------------------------------------------------------------------------------------------------------

def note_card(kind: str, name: str, *, fingerprint: str, verdict: str, p_value: float | None, family: str = "", trials: int = 0,
              details: Mapping[str, Any] | None = None) -> bool:
    """Note a card once per identical input (returns False when already noted). `trials` > 0 also counts trials."""
    study = f"{kind}_card:{name}"
    if any(row.get("study") == study and row.get("fingerprint") == fingerprint for row in entries("card")):
        return False
    append({"type": "card", "kind": kind, "study": study, "name": name, "family": family or name, "fingerprint": fingerprint, "verdict": verdict,
            "p": None if p_value is None or not np.isfinite(p_value) else float(p_value), "details": dict(details or {})})
    if trials:
        record_trials(study, trials, family=family or name, data=kind, details={"fingerprint": fingerprint})
    return True


def latest_cards(kind: str | None = None) -> dict[str, dict[str, Any]]:
    """The newest card per name (of one kind)."""
    out: dict[str, dict[str, Any]] = {}
    for row in entries("card"):
        if kind is None or row.get("kind") == kind:
            out[f"{row['kind']}:{row['name']}"] = row
    return out


def lab_summary() -> pd.DataFrame:
    """Per family: trials, cards, cards per verdict. The lab-level view of how many ideas were tried and how many held."""
    rows: dict[str, dict[str, Any]] = {}
    for row in entries("trials"):
        rows.setdefault(row.get("family") or "(none)", {})
        rows[row.get("family") or "(none)"]["trials"] = rows[row.get("family") or "(none)"].get("trials", 0) + int(row["configurations"])
    for row in latest_cards().values():
        family = row.get("family") or "(none)"
        rows.setdefault(family, {})
        rows[family]["cards"] = rows[family].get("cards", 0) + 1
        rows[family][row["verdict"]] = rows[family].get(row["verdict"], 0) + 1
    table = pd.DataFrame(rows).T.fillna(0).astype(int) if rows else pd.DataFrame()
    if len(table):
        table.loc["all"] = table.sum()
    return table


# --- pre-registrations -----------------------------------------------------------------------------------------------

class PreregNotLocked(RuntimeError):
    """A study was started before its pre-registration was written and locked, or after its text changed."""


def prereg_path(hypothesis: str) -> Path:
    return Path(os.environ.get("QUANTLAB_PREREG_DIR", "research/prereg")) / f"{hypothesis}.txt"


def _prereg_hash(hypothesis: str) -> str:
    return hashlib.sha256(prereg_path(hypothesis).read_bytes()).hexdigest()


def prereg_locks(hypothesis: str) -> list[dict[str, Any]]:
    return [row for row in entries("prereg") if row.get("hypothesis") == hypothesis]


def lock_prereg(hypothesis: str) -> dict[str, Any]:
    """Record the text's SHA-256. The same text again changes nothing; a changed text is a new, visible version."""
    path = prereg_path(hypothesis)
    if not path.exists():
        raise FileNotFoundError(f"no pre-registration at {path}")
    if PLACEHOLDER in path.read_text(encoding="utf-8"):
        raise PreregNotLocked(f"{path} still has {PLACEHOLDER} fields: fill every section in before locking")
    digest, locks = _prereg_hash(hypothesis), prereg_locks(hypothesis)
    if locks and locks[-1]["sha256"] == digest:
        return locks[-1]
    return append({"type": "prereg", "hypothesis": hypothesis, "sha256": digest, "version": len(locks) + 1, "path": str(path)})


def prereg_status(hypothesis: str) -> str:
    """missing | unlocked | locked | changed."""
    if not prereg_path(hypothesis).exists():
        return "missing"
    locks = prereg_locks(hypothesis)
    if not locks:
        return "unlocked"
    return "locked" if locks[-1]["sha256"] == _prereg_hash(hypothesis) else "changed"


def require_prereg(hypothesis: str) -> dict[str, Any]:
    """The current lock; raises PreregNotLocked unless the text on disk is the locked one."""
    status = prereg_status(hypothesis)
    if status != "locked":
        raise PreregNotLocked(f"pre-registration {hypothesis} is {status}: write it, fill it in, and lock it (quantlab.validation.ledger.lock_prereg)")
    return prereg_locks(hypothesis)[-1]


# --- the frozen holdout -----------------------------------------------------------------------------------------------

class HoldoutRefused(RuntimeError):
    """A second look at the frozen holdout for the same candidate, without a stated reason."""


def holdout_looks(candidate: str | None = None) -> list[dict[str, Any]]:
    return [row for row in entries("holdout_look") if candidate is None or row.get("candidate") == candidate]


@contextlib.contextmanager
def holdout_look(candidate: str, *, approved_by: str, again: str | None = None) -> Iterator[dict[str, Any]]:
    """Open the frozen holdout inside this block for one candidate's final test (logged; once per candidate)."""
    global _open_look
    if not candidate.strip() or not approved_by.strip():
        raise ValueError("name the candidate and who approved the look")
    previous = holdout_looks(candidate)
    if previous and not again:
        raise HoldoutRefused(f"{candidate!r} already had its look on {previous[0]['at']}; pass again='<reason>' to look again (it is logged)")
    record = append({"type": "holdout_look", "candidate": candidate, "approved_by": approved_by, "look": len(previous) + 1, "again": again})
    before, _open_look = _open_look, candidate
    try:
        yield record
    finally:
        _open_look = before


def holdout_cut(item: pd.Series | pd.DataFrame) -> pd.Series | pd.DataFrame:
    """The data before the frozen holdout, unless a `holdout_look` block is open."""
    if _open_look is not None or not isinstance(item.index, pd.DatetimeIndex) or not len(item):
        return item
    index = item.index.tz_convert(None) if item.index.tz is not None else item.index
    return item[index < FROZEN_FROM]
