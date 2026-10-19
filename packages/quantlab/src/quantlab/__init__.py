"""quantlab: a portable research-to-live lab for systematic multi-asset strategies.

One engine from idea to live capital, causal by construction and statistics-first. The plan and its checklist are in
docs/research_lab_plan.md of the repository this package started in; the conventions every component follows are in
CONVENTIONS.md next to this package.

This package depends only on numpy, pandas, scipy and matplotlib. It never imports the trading runtime that hosts it:
a host may import quantlab, never the reverse (tests/test_boundary.py enforces this).
"""

__version__ = "0.1.0"
