"""The package boundary (plan 0.1): quantlab imports nothing from its host repository and nothing beyond its declared
dependencies, so it can move to another firm's infrastructure unchanged."""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
SOURCE = PACKAGE / "src" / "quantlab"
DECLARED = {"numpy", "pandas", "scipy", "matplotlib"}
OPTIONAL = {"statsmodels", "arch", "exchange_calendars", "IPython"}  # extras: may only be imported inside functions
HOST_MODULES = {"src", "config", "main", "kill_switch", "scripts", "tests"}


def _imports(path: Path) -> list[tuple[str, int, bool]]:
    """(top-level module, line, imported at module level) for every import in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top_level = {id(node) for node in tree.body}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name.split(".")[0], node.lineno, id(node) in top_level) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.module.split(".")[0], node.lineno, id(node) in top_level))
    return found


def test_quantlab_never_imports_its_host_or_undeclared_packages() -> None:
    allowed = DECLARED | OPTIONAL | {"quantlab"} | set(sys.stdlib_module_names)
    problems = []
    for path in SOURCE.rglob("*.py"):
        for module, line, at_top in _imports(path):
            where = f"{path.relative_to(PACKAGE)}:{line}"
            if module in HOST_MODULES:
                problems.append(f"{where} imports the host module {module!r}")
            elif module not in allowed:
                problems.append(f"{where} imports {module!r}, which is not a declared dependency")
            elif module in OPTIONAL and at_top:
                problems.append(f"{where} imports the optional {module!r} at module level (import it inside the function)")
    assert not problems, "\n".join(problems)


def test_declared_dependencies_match_pyproject() -> None:
    project = tomllib.loads((PACKAGE / "pyproject.toml").read_text())["project"]
    names = {item.split(">")[0].split("=")[0].split("<")[0].strip() for item in project["dependencies"]}
    assert names == DECLARED


def test_no_machine_specific_paths_or_forced_backends_in_the_source() -> None:
    for path in SOURCE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text and "C:\\" not in text, f"{path}: an absolute, machine-specific path"
        assert 'matplotlib.use(' not in text, f"{path}: never force a matplotlib backend (notebooks need inline)"


def test_the_golden_numbers_file_is_complete() -> None:
    import json

    golden = json.loads((PACKAGE / "tests" / "golden" / "studies.json").read_text())
    studies = {key: value for key, value in golden.items() if not key.startswith("_")}
    assert {"H11", "H13", "H14", "crypto_sleeve_cards"} <= set(studies)
    for name, study in studies.items():
        assert study["source"] and study["values"] and study["tolerance"] > 0, name
        assert all(isinstance(value, (int, float)) for value in study["values"].values()), name
