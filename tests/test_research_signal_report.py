"""The standard signal report (src/research/signal_report.py), pre-registration locks and the hypothesis scaffold."""

from __future__ import annotations

import importlib.util
import sys

import numpy as np
import pandas as pd
import pytest

from src.research import governance
from src.research.signal_report import signal_report
from test_research_events import _planted


def test_the_report_runs_every_screen_and_writes_one_page(tmp_path) -> None:
    frame, events, vol = _planted()
    feature = np.zeros(len(frame))
    feature[events] = 1.0
    report = signal_report("Planted drift", frame, events, interval_seconds=3600, horizons=(1, 5, 20), primary_horizon=5, matching=vol, cost_bps=10.0,
                           variants={"one bar later": events + 1, "two bars later": events + 2}, feature=feature, notes=["a note <b>"], bootstrap_runs=300)
    assert set(report.tables) == {"data quality", "frequency", "events per year", "event study", "regimes", "robustness", "feature information"}
    assert report.trials == 3 and report.tables["robustness"].index.tolist() == ["as defined", "one bar later", "two bars later"]
    assert report.tables["robustness"]["mean"].is_monotonic_decreasing and report.tables["data quality"].loc["missing_bars", "value"] == 0
    assert report.tables["events per year"]["events"].sum() == len(events)
    for heading in ("Around the event", "By horizon", "Event study", "How often it fires", "By regime at the event (5-bar return)", "Nearby definitions (5-bar return)",
                    "The feature behind the signal", "Data quality"):
        assert f"<h2>{heading}</h2>" in report.html, heading
    assert "a note &lt;b&gt;" in report.html and "after 10 bps of costs" in report.html and "Matched controls returned" in report.html
    assert "100% of the 3 definitions agree in sign" in report.html and report.html.count("data:image/png;base64,") == 3
    page = report.write(tmp_path / "out")
    assert page.read_text() == report.html and (tmp_path / "out" / "event_study.csv").exists() and (tmp_path / "out" / "events.csv").exists()


def test_a_signal_that_never_fires_still_gets_a_report_and_a_wrong_primary_horizon_is_refused() -> None:
    frame, _events, _vol = _planted(bars=600)
    report = signal_report("Nothing", frame, [], interval_seconds=3600, horizons=(1, 5), bootstrap_runs=100)
    assert "The signal never fired in this sample." in report.html and "regimes" not in report.tables and report.trials == 1
    assert "No matched controls" not in report.html and "<h2>Around the event</h2>" not in report.html
    with pytest.raises(ValueError, match="primary_horizon"):
        signal_report("x", frame, [100], interval_seconds=3600, horizons=(1, 5), primary_horizon=7)


@pytest.fixture
def prereg_dir(tmp_path, monkeypatch):
    folder = tmp_path / "research" / "prereg"
    monkeypatch.setenv("CQM_PREREG_DIR", str(folder))
    return folder


def _scaffold():
    spec = importlib.util.spec_from_file_location("new_hypothesis", "scripts/research/new_hypothesis.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_pre_registration_must_be_filled_in_and_locked_before_its_study_runs(prereg_dir, tmp_path) -> None:
    scaffold = _scaffold()
    assert governance.prereg_status("H9") == "missing"
    with pytest.raises(governance.PreregNotLocked, match="is missing"):
        governance.require_prereg("H9")
    prereg, study = scaffold.new("H9", 'Dips "reverse"', study_dir=tmp_path)
    assert prereg == prereg_dir / "H9.txt" and study == tmp_path / "h9_study.py"
    text = prereg.read_text()
    assert text.startswith('PRE-REGISTRATION H9: DIPS "REVERSE"') and "KILL IF" in text and "PRIMARY OUTCOME" in text
    assert governance.prereg_status("H9") == "unlocked"
    with pytest.raises(governance.PreregNotLocked, match="still has TODO"):
        governance.lock_prereg("H9")
    with pytest.raises(SystemExit, match="already there"):
        scaffold.new("H9", "again", study_dir=tmp_path)
    with pytest.raises(SystemExit, match="upper-case"):
        scaffold.new("h9", "lower", study_dir=tmp_path)

    prereg.write_text(text.replace("TODO", "Decided"))
    first = governance.lock_prereg("H9")
    assert first["version"] == 1 and governance.prereg_status("H9") == "locked" and governance.require_prereg("H9")["sha256"] == first["sha256"]
    assert governance.lock_prereg("H9") == first and len(governance.prereg_locks("H9")) == 1  # locking the same text again changes nothing
    prereg.write_text(prereg.read_text() + "\nA threshold moved after seeing results.\n")
    assert governance.prereg_status("H9") == "changed"
    with pytest.raises(governance.PreregNotLocked, match="changed after its lock"):
        governance.require_prereg("H9")
    assert governance.lock_prereg("H9")["version"] == 2 and governance.prereg_status("H9") == "locked"  # allowed, and on record
    [line] = scaffold.status_lines()
    assert line.startswith("H9") and "locked v2" in line and "no report yet" in line


def test_the_generated_study_runs_end_to_end_once_its_event_is_defined(prereg_dir, tmp_path, monkeypatch, capsys) -> None:
    scaffold = _scaffold()
    prereg, study_file = scaffold.new("H8", "Planted drift", study_dir=tmp_path)
    spec = importlib.util.spec_from_file_location("h8_study", study_file)
    study = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(study)
    frame, events, _vol = _planted()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["h8_study.py"])
    monkeypatch.setattr(study, "COINS", ("BTC",))
    monkeypatch.setattr(study, "HORIZONS", (1, 5))
    monkeypatch.setattr(study, "PRIMARY_HORIZON", 5)
    monkeypatch.setattr(study, "load_frame", lambda coin: frame)
    with pytest.raises(governance.PreregNotLocked, match="is unlocked"):
        study.main()  # the text isn't locked: no result may exist yet
    prereg.write_text(prereg.read_text().replace("TODO", "Decided"))
    governance.lock_prereg("H8")
    with pytest.raises(NotImplementedError, match="define the event"):
        study.main()
    monkeypatch.setattr(study, "define_events", lambda frame: events)
    monkeypatch.setattr(study, "variants", lambda frame: {"later": events + 1})
    study.main()
    out = capsys.readouterr().out
    assert "BTC: 47 events, 5-bar mean" in out and "Judge it against the PASS CRITERIA" in out
    [folder] = list((tmp_path / "data" / "research").glob("h8_*"))
    assert (folder / "BTC" / "report.html").exists() and (folder / "manifest.json").exists()
    trials = [entry for entry in governance.ledger_entries() if entry.get("type") == "trials" and entry.get("family") == "H8"]
    assert len(trials) == 1 and trials[0]["configurations"] == 2  # the definition and its one variant
