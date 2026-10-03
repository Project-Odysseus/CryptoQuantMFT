"""Start a hypothesis the same way every time: a pre-registration to fill in, a study script, and a lock.

    python scripts/research/new_hypothesis.py new H4 "Drawdowns of 10% reverse within a week"
    # ... fill in every TODO in research/prereg/H4.txt and in scripts/research/h4_study.py ...
    python scripts/research/new_hypothesis.py lock H4       # records the text's hash in the trial ledger
    python scripts/research/h4_study.py                     # refuses to run until the text is locked
    python scripts/research/new_hypothesis.py status        # every hypothesis and where it stands

Why the lock: a rule chosen after seeing results always looks good. The pre-registration says, before any number
exists, what should be seen, how it is measured and what would reject it. The lock stores the text's SHA-256 in the
trial ledger; the study checks it on every run. Changing the text afterwards is allowed, but it is a new version,
locked again and visible in the ledger.

The study script it writes uses the standard tools (`src/research/events.py`, `src/research/signal_report.py`), so
the only things to write are how the event is defined and which data it needs.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import governance  # noqa: E402

PREREG = """PRE-REGISTRATION {id}: {title_upper}
Version 1. Written {date}, before any {id} test was run. Changes after results = a new version (lock it again), logged
as extra trials.

PRIOR EXPOSURE (what has already been looked at that bears on this)
TODO: studies in docs/research_log.md that touched this idea or its data. "None" is an answer.

MARKET BEHAVIOUR
TODO: the behaviour in one or two sentences.

MECHANISM
TODO: why it should exist, and who is on the other side of the trade.

OBSERVABLE PREDICTION AND EXPECTED SIGN
TODO: what should be visible in the data if the hypothesis is true, with its sign.

DATA (point-in-time; src/research/pit.py)
TODO: series, venues and bar interval. Development window: first usable date -> 2025-12-31. The holdout (2026-01-01 on)
is locked and is run once, at the end, only with the owner's approval.

SIGNAL / EVENT DEFINITION (known at a bar's close; the position is taken at the next bar's open)
TODO: the exact rule, with every number in it. No future information.

PRIMARY OUTCOME
TODO: one metric at one horizon (e.g. the mean 5-bar log return after the event, minus matched controls).

SECONDARY OUTCOMES
TODO: other horizons, hit rate, worst excursion, per coin and per year.

TESTS
TODO: event study against volatility-matched controls with bootstrap intervals (src/research/events.py); regimes;
placebo; costs at 1x and 2x.

PARAMETER GRID (every configuration is a trial; count them)
TODO: the nearby definitions that will be run, all listed here. Nothing outside this list is run.

PASS CRITERIA
TODO: what must hold on the primary outcome, in numbers.

KILL IF
TODO: what result rejects the hypothesis.

KNOWN RISKS AND BIASES
TODO: costs and capacity, overlap with the sleeves already in the book, regime dependence, small samples.
"""

STUDY = '''"""{id}, {title}: run the pre-registered study (research/prereg/{id}.txt) on development data.

    python scripts/research/{slug}_study.py      # writes data/research/{slug}_<time>/ (one report per coin, tables, manifest)

Written by scripts/research/new_hypothesis.py. Fill in the settings and `define_events` from the pre-registration,
lock it (`python scripts/research/new_hypothesis.py lock {id}`), then run. Every configuration run is logged to the
trial ledger (family "{id}"). The holdout stays locked.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import governance, pit  # noqa: E402
from src.research.events import first_crossings  # noqa: E402,F401 - the usual way to turn a condition into events
from src.research.signal_report import signal_report  # noqa: E402

HYPOTHESIS = "{id}"
# From the pre-registration. Change them there first (a new version), never here alone.
COINS = ("BTC", "ETH", "SOL")
INTERVAL = "1h"
INTERVAL_SECONDS = 3600
HORIZONS = (1, 6, 24, 72)  # bars
PRIMARY_HORIZON = 24
SIDE = 1.0  # +1: long after the event; -1: short
COST_BPS = 20.0  # one round trip at taker fees and spread
VOL_WINDOW = 24  # bars of trailing realized volatility the controls are matched on


def load_frame(coin: str) -> pd.DataFrame:
    """The coin's bars, indexed by open time (holdout-locked)."""
    return pit.load_bars(coin, INTERVAL)


def define_events(frame: pd.DataFrame) -> np.ndarray:
    """Bar positions where the pre-registered condition occurs, each known at that bar's close.

    Example: a close at least 10% below the highest of the previous 480 closes, counted once per 72 bars:
        peak = frame["close"].shift(1).rolling(480).max()
        return first_crossings(frame["close"] <= peak * 0.90, min_gap=72)
    """
    raise NotImplementedError("define the event from research/prereg/{id}.txt")


def variants(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    """The nearby definitions listed in the pre-registration's parameter grid (label -> positions); empty if none."""
    return {{}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-ledger", action="store_true", help="Don't log the trials (for a rerun after a code fix, not for new tries)")
    args = parser.parse_args()
    lock = governance.require_prereg(HYPOTHESIS)
    out = Path("data/research") / f"{slug}_{{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}}"
    governance.write_manifest(out, args=args, extra={{"prereg_version": lock["version"], "prereg_sha256": lock["sha256"]}})
    trials = 0
    for coin in COINS:
        frame = load_frame(coin)
        matching = np.log(frame["close"]).diff().rolling(VOL_WINDOW).std().to_numpy()
        report = signal_report(f"{{HYPOTHESIS}} {{coin}}: {title}", frame, define_events(frame), interval_seconds=INTERVAL_SECONDS, horizons=HORIZONS,
                               primary_horizon=PRIMARY_HORIZON, side=SIDE, matching=matching, cost_bps=COST_BPS, variants=variants(frame),
                               notes=[f"Pre-registration {{HYPOTHESIS}} v{{lock['version']}}; development data {{frame.index[0]:%Y-%m-%d}} to {{frame.index[-1]:%Y-%m-%d}}."])
        page = report.write(out / coin)
        trials = max(trials, report.trials)  # one definition tried on several coins is one trial, not one per coin
        row = report.study.summary.loc[PRIMARY_HORIZON]
        print(f"{{coin}}: {{int(row['events'])}} events, {{PRIMARY_HORIZON}}-bar mean {{row['mean']:+.2%}} ({{row['ci_low']:+.2%}} to {{row['ci_high']:+.2%}}), "
              f"excess over controls {{row['excess']:+.2%}} -> {{page}}")
    if not args.no_ledger:
        governance.record_trials(f"{slug}_study", trials, family=HYPOTHESIS, data=f"binance {{INTERVAL}} to 2025-12-31", details={{"coins": list(COINS), "horizons": list(HORIZONS)}})
    print(f"Wrote {{out}}. Judge it against the PASS CRITERIA and KILL IF in research/prereg/{id}.txt, then write research/reports/{id}.md and the research log entry.")


if __name__ == "__main__":
    main()
'''


def study_path(hypothesis: str) -> Path:
    return Path(__file__).resolve().parent / f"{hypothesis.lower()}_study.py"


def new(hypothesis: str, title: str, *, study_dir: Path | None = None) -> tuple[Path, Path]:
    """Write the pre-registration template and the study script; refuses to overwrite either."""
    if not re.fullmatch(r"[A-Z][A-Z0-9]{0,11}", hypothesis):
        raise SystemExit("the id must be short and upper-case, like H4")
    prereg = governance.prereg_path(hypothesis)
    study = (study_dir or study_path(hypothesis).parent) / f"{hypothesis.lower()}_study.py"
    existing = [str(path) for path in (prereg, study) if path.exists()]
    if existing:
        raise SystemExit(f"already there, not overwritten: {', '.join(existing)}")
    prereg.parent.mkdir(parents=True, exist_ok=True)
    prereg.write_text(PREREG.format(id=hypothesis, title_upper=title.upper(), date=f"{datetime.now(timezone.utc):%Y-%m-%d}"), encoding="utf-8")
    study.write_text(STUDY.format(id=hypothesis, slug=hypothesis.lower(), title=title.replace('"', "'")), encoding="utf-8")
    return prereg, study


def status_lines() -> list[str]:
    """One line per pre-registration on disk: its lock state and whether a report exists."""
    folder = governance.prereg_path("x").parent
    lines = []
    for path in sorted(folder.glob("*.txt")):
        hypothesis = path.stem
        state = governance.prereg_status(hypothesis)
        locks = governance.prereg_locks(hypothesis)
        version = f" v{locks[-1]['version']} ({locks[-1]['at'][:10]})" if locks else ""
        report = "report written" if (folder.parent / "reports" / f"{hypothesis}.md").exists() else "no report yet"
        title = path.read_text(encoding="utf-8").splitlines()[0].split(":", 1)[-1].strip().capitalize()
        lines.append(f"{hypothesis:<6} {state + version:<24} {report:<16} {title}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("new", help="Write the pre-registration template and the study script")
    create.add_argument("hypothesis", help="A short id, e.g. H4")
    create.add_argument("title", help="The hypothesis in a few words")
    lock = commands.add_parser("lock", help="Record the filled-in pre-registration's hash in the trial ledger")
    lock.add_argument("hypothesis")
    commands.add_parser("status", help="List every hypothesis and whether it is locked")
    args = parser.parse_args()
    if args.command == "new":
        prereg, study = new(args.hypothesis, args.title)
        print(f"Wrote {prereg} and {study}.\nNext: fill in every TODO in the pre-registration, then `define_events` and the settings in the study, then\n"
              f"  python scripts/research/new_hypothesis.py lock {args.hypothesis}\n  python {study}")
    elif args.command == "lock":
        try:
            entry = governance.lock_prereg(args.hypothesis)
        except (FileNotFoundError, governance.PreregNotLocked) as exc:
            print(exc)
            return 1
        print(f"{args.hypothesis} locked as version {entry['version']} ({entry['sha256'][:12]}).")
    else:
        lines = status_lines()
        print("\n".join(lines) if lines else "No pre-registrations yet: python scripts/research/new_hypothesis.py new H1 \"title\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
