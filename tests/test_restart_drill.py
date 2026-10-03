"""The restart drill (scripts/drills/restart_drill.py) with real processes and real SIGKILLs, in a short form."""

from __future__ import annotations

import subprocess
import sys


def test_the_runtime_survives_kill_9_inside_a_decision() -> None:
    """Two kill points that used to leave the book and the paper exchange apart, plus one kill from outside."""
    result = subprocess.run([sys.executable, "scripts/drills/restart_drill.py", "config/portfolio.example.toml", "--bars", "90", "--counts", "2",
                             "--points", "order_sent", "before_checkpoint", "--random-kills", "1"], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "order_sent:2" in result.stdout and "FAIL" not in result.stdout and "3 passed" in result.stdout
