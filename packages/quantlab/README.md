# quantlab

A portable research-to-live lab for systematic multi-asset strategies: one engine from idea to live capital, causal
by construction, statistics-first.

- **Plan and checklist:** `docs/research_lab_plan.md` in the host repository.
- **Conventions every component follows:** `CONVENTIONS.md`.
- **Ownership and trading restrictions:** `COMPLIANCE.md`.

## Install

```bash
pip install -e packages/quantlab              # from the host repository's root
pip install -e "packages/quantlab[test]"      # with pytest
pytest packages/quantlab/tests
```

Core dependencies: numpy, pandas, scipy, matplotlib (tested versions in `requirements.lock`).

## Status

Phase 0 (foundations):
- the package boundary;
- the strict config loader (`quantlab.config`);
- the conventions as code (`quantlab.conventions`).

The engine, data layer and regime work follow the plan's phases.
