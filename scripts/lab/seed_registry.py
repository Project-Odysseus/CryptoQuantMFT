"""Seed the lab's hypothesis registry (quantlab.tracking.store) from this project's pre-registrations and reports.

Titles come from the first line of research/prereg/H*.txt; the decision text is the verdict line of
research/reports/H*.md. Statuses are read off those reports by hand and kept here, because the reports word their
verdicts differently (FAIL, KILL, INCONCLUSIVE, outcome D). Running it again updates titles and changes a status only
where it differs, with the reason recorded in the hypothesis's history.

    python scripts/lab/seed_registry.py            # into $QUANTLAB_DATA/research/lab.db
    python scripts/lab/seed_registry.py --dry-run  # print what it would write
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

PREREG = Path("research/prereg")
REPORTS = Path("research/reports")
PROPER = ("BTC", "ETH", "US", "Kraken", "Kalshi", "Deribit", "Nasdaq-100", "S&P", "STOXX", "Europe", "IV", "Phase A")

# From research/reports/*.md (verdict sections) and TODO.MD, 2026-10-19.
STATUS = {
    "H1": "failed", "H2": "failed", "H3": "failed", "H4": "pre-registered", "H5": "pre-registered", "H6": "inconclusive", "H7": "inconclusive",
    "H8": "failed", "H9": "failed", "H10": "inconclusive", "H11": "inconclusive", "H12": "inconclusive", "H13": "paper", "H14": "failed",
}
NOTES = {
    "H4": "Confirmatory run on or after 2026-11-17 (TODO.MD).",
    "H5": "Confirmatory run on or after 2026-11-17 (TODO.MD).",
    "H13": "Passed its pre-registered criteria, but its strategy card says no evidence (t 1.30, timing placebo failed). Paper book: config/portfolio.equity_trend_paper.toml.",
}


def title(hypothesis: str) -> str:
    first = (PREREG / f"{hypothesis}.txt").read_text(encoding="utf-8").splitlines()[0]
    text = re.sub(r"^PRE-REGISTRATION\s+H\d+:\s*", "", first).strip().lower()
    for word in PROPER:
        text = re.sub(rf"(?<![\w-]){re.escape(word.lower())}(?![\w])", word, text)
    return text[:1].upper() + text[1:]


def decision(hypothesis: str) -> str:
    path = REPORTS / f"{hypothesis}.md"
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):  # a verdict section first, then the first bold verdict line
        if re.match(r"##\s.*verdict", line, re.IGNORECASE):
            following = next((item for item in lines[index + 1:] if item.strip()), "")
            return re.sub(r"\*\*", "", following.strip("- ").strip())
    for line in lines:
        if re.match(r"\s*(-\s*)?\*\*.*(verdict|outcome|fail|pass|kill)", line, re.IGNORECASE):
            return re.sub(r"\*\*", "", line.strip("- ").strip())
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    hypotheses = sorted((path.stem for path in PREREG.glob("H*.txt")), key=lambda name: int(name[1:]))
    if args.dry_run:
        for hypothesis in hypotheses:
            print(f"{hypothesis:4s} {STATUS.get(hypothesis, 'pre-registered'):15s} {title(hypothesis)[:70]}\n     {decision(hypothesis)[:140]}")
        return
    from quantlab.tracking.store import ExperimentStore

    store = ExperimentStore()
    existing = store.hypotheses()
    for hypothesis in hypotheses:
        status = STATUS.get(hypothesis, "pre-registered")
        store.register(hypothesis, title(hypothesis), status="pre-registered", notes=NOTES.get(hypothesis, ""))
        current = existing.at[hypothesis, "status"] if hypothesis in existing.index else "pre-registered"
        if status != current or hypothesis not in existing.index:
            store.set_status(hypothesis, status, reason=f"seeded from {REPORTS / (hypothesis + '.md')}", decision=decision(hypothesis))
    print(store.lab_state()[["status", "title"]].to_string())


if __name__ == "__main__":
    main()
