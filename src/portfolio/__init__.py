"""Portfolio layer: several strategies (sleeves) on several instruments, combined into one book.

See docs/portfolio_plan.md for the design and the build steps. Modules here are
pure (no network, no database, no clock) except `engine.py`, so research,
runtime and tests share the same decisions.
"""
