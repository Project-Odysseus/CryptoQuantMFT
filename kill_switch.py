"""Convenience wrapper for triggering the runtime kill switch."""

from __future__ import annotations

import sys

from main import main


if __name__ == "__main__":
    sys.argv = ["main.py", "--kill-switch", *sys.argv[1:]]
    main()
