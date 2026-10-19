"""Every lab test writes its ledger to a temporary file, never to the real research record."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QUANTLAB_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("QUANTLAB_PREREG_DIR", str(tmp_path / "prereg"))
