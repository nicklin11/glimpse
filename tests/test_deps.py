#!/usr/bin/env python3
"""Enforce ADR-0002 D2: third-party code may import numpy and nothing else.

ADR-0001 D2 is a decision record, and a decision record does not enforce itself. The
zipapp ships one file with nothing to resolve at start, which is exactly what a
stray `import requests` gives away -- silently, at runtime, on someone else's
machine, after the fact. This script is the executable form of that sentence.

It walks the AST of every module under `src/` and reports any import that is
neither standard library nor numpy. Two deliberate properties:

  * AST, not text. A match on the string "requests" inside a docstring or a
    comment is not an import, and a check that cannot tell the difference will
    be disabled the first time it produces a false positive.
  * `sys.stdlib_module_names`, not a hand-kept list. A hand-kept list is a
    fourth table of facts in this repo, and every one of them has drifted.

It also cross-checks `pyproject.toml` against the source: a dependency that is
declared but never imported, or imported but never declared, is the same drift
in the other direction, and the zipapp build depends on both halves agreeing.
"""

import ast
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

SAVED_ENV = _env.isolate()

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
PKG = SRC / "glimpse"

# The one permitted third-party import, and the reason it is permitted.
ALLOWED = {"numpy"}
# The package under test. Everything below `src/glimpse/` may import it freely;
# treating it as third-party would flag every module in the codebase.
SELF = {"glimpse"}
# Sibling modules in `tests/` itself. `tests/_env.py` is not a dependency the zipapp
# would have to ship -- it is the file next door, which is a different property. Before
# #63's fix each suite duplicated the env handling inline; sharing it means the suites now
# import a local module, and this rule would otherwise report three of its own files.
SIBLINGS = {path.stem for path in (REPO / "tests").glob("*.py")} - {"test_deps"}

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


def top_level(name: str) -> str:
    """`a.b.c` -> `a`. Relative imports have no root and return ''."""
    return name.split(".", 1)[0]


def imports_of(tree: ast.AST) -> list[tuple[str, int]]:
    """Every module named by an import statement, as (top-level name, line).

    Relative imports (`from .x import y`) are skipped outright. For those,
    `ImportFrom.module` is only the *tail* -- `from .cli import main` yields
    "cli", not "glimpse.cli" -- so it names a module that does not exist at any
    absolute path. And `level > 0` cannot escape the package by definition,
    which is exactly what this check is about.
    """
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((top_level(alias.name), node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((top_level(node.module), node.lineno))
    return found


# --- 1. no import outside stdlib + numpy -------------------------------------

modules = sorted(PKG.rglob("*.py"))
check("there are modules to scan", bool(modules), f"{len(modules)} files")

imported: set[str] = set()
offenders: list[tuple[str, int, str]] = []

for path in modules:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for name, lineno in imports_of(tree):
        if not name or name in sys.stdlib_module_names or name in ALLOWED or name in SELF:
            if name:
                imported.add(name)
            continue
        offenders.append((path.relative_to(REPO).as_posix(), lineno, name))

check(
    "no third-party import outside numpy",
    not offenders,
    "; ".join(f"{p}:{ln} imports {n}" for p, ln, n in offenders),
)

# --- 2. numpy is actually used -----------------------------------------------
# The opposite drift: ADR-0001 D2 permits numpy, so a future edit that drops the import
# leaves a declared dependency nobody needs, and the zipapp pays for it.
check(
    "numpy is imported somewhere, so the declared dependency is real",
    "numpy" in imported,
    f"imported: {', '.join(sorted(imported & ALLOWED)) or 'nothing'}",
)

# --- 3. pyproject and the source agree ---------------------------------------

declared = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
declared_deps = {
    top_level(re.split(r"[<>=!~\[ ]", spec, maxsplit=1)[0])
    for spec in declared["project"]["dependencies"]
}
check(
    "pyproject declares exactly numpy",
    declared_deps == ALLOWED,
    f"declared: {sorted(declared_deps)}",
)
check(
    "every declared dependency is actually imported",
    declared_deps <= imported,
    f"declared but unused: {sorted(declared_deps - imported)}",
)

requires_python = declared["project"].get("requires-python", "")
check(
    "requires-python pins the floor the CI matrix tests", "3.11" in requires_python, requires_python
)

# --- 4. the scripts under tests/ hold themselves to the same rule ------------
# They import the package under test and the stdlib. If one ever grows a real
# dependency, it is still a dependency the zipapp does not ship.

for path in sorted((REPO / "tests").rglob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = [
        (path.relative_to(REPO).as_posix(), lineno, name)
        for name, lineno in imports_of(tree)
        if name and name not in sys.stdlib_module_names and name not in ALLOWED | SELF | SIBLINGS
    ]
    check(
        f"tests/{path.name} imports no dependency outside numpy",
        not bad,
        "; ".join(f"{p}:{ln} imports {n}" for p, ln, n in bad),
    )

_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all dependency checks passed")
