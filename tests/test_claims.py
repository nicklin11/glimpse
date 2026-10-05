#!/usr/bin/env python3
"""The remaining invariants from #48: claims that must match the code.

Issue #48 listed five checks and shipped one. Four were left, and one of the
four -- the stage-numbering one -- was written against an API that does not
exist: it asserted "every stage module declares STAGE = n", and exactly one of
twelve does. A check written from an assumption rather than from the source
produces either a false alarm or a check that silently guards nothing.

So each invariant below is derived from the thing it is supposed to describe,
and each states where its truth lives:

    stage numbering   pipeline.py's StageReport calls, and stages.IMPLEMENTED
    exit codes        exitcodes.DESCRIPTIONS
    repo tree         a literal allowlist, because the point is to catch surprises
    shipboard         the import graph, not the word

The rule these share: a hand-maintained table in prose is a claim, and the only
thing that makes it true is a check that fails when it stops being true.
"""

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

SAVED_ENV = _env.isolate()


from glimpse import exitcodes as ec  # noqa: E402
from glimpse import stages as gst  # noqa: E402

README = (REPO / "README.md").read_text(encoding="utf-8")
PIPELINE = (REPO / "src" / "glimpse" / "pipeline.py").read_text(encoding="utf-8")

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


# --- 2. stage numbering -------------------------------------------------------
# #48's version asserted `IMPLEMENTED` was a collection. It is an int. The real
# invariant is that the progress numbers the pipeline emits are 1..IMPLEMENTED,
# each exactly once -- a duplicate or a gap is the `[6/12] caption` bug where
# stage 11 printed the total for its own number.

reported = [int(n) for n in re.findall(r"StageReport\(\s*(\d+)", PIPELINE)]
check(
    "the pipeline reports every stage exactly once, with no gap",
    reported == list(range(gst.FIRST_STAGE, gst.IMPLEMENTED + 1)),
    f"reported {reported}, expected {list(range(gst.FIRST_STAGE, gst.IMPLEMENTED + 1))}",
)
check(
    "stages.IMPLEMENTED agrees with what the pipeline reports",
    len(reported) == gst.IMPLEMENTED,
    f"IMPLEMENTED={gst.IMPLEMENTED}, pipeline reports {len(reported)}",
)

# The README status table, *if it exists*, must have one row per stage plus doctor, and no
# stage number may appear twice. A duplicate is how "6" ended up on two rows.
#
# The check is conditional because the README no longer carries per-stage status. `c4aab27`
# cut it on the grounds that development state belongs in the issues, and this assertion was
# never updated to match -- it kept failing against a table that had been deleted on purpose,
# and a permanently red suite is a guard nobody reads. The invariant it protects still holds
# wherever such a table appears; what it no longer asserts is that one must appear.
#
# Scoped to the section: `^\| (\d+) \| ` matches the exit-code table too, and an
# unscoped scan of a document with three numeric tables reports all three.
if "| stage |" in README:
    status_start = README.index("| stage |")
    status_end = README.index("\n## ", status_start)
    status_rows = re.findall(r"^\| (\d+) \| ", README[status_start:status_end], flags=re.MULTILINE)
    check(
        "the README status table has one row per stage, plus doctor",
        sorted(int(n) for n in status_rows) == list(range(0, gst.IMPLEMENTED + 1)),
        f"stage column reads {status_rows}",
    )
else:
    check(
        "the README carries no per-stage status table, which the issues own",
        True,
        "the table exists and the conditional branch above will judge it",
    )

# --- 3. exit-code table -------------------------------------------------------
# The table is prose next to code. `DESCRIPTIONS` exists so the comparison does
# not depend on mapping constant names to codes by hand.
table_codes = {
    int(m) for m in re.findall(r"^\| (\d+) \| .+ \|$", README.split("## Exit codes")[1], flags=re.M)
}
check(
    "the README exit-code table lists exactly the codes exitcodes defines",
    table_codes == set(ec.DESCRIPTIONS),
    f"README {sorted(table_codes)}, code {sorted(ec.DESCRIPTIONS)}",
)

# --- 4. repo tree allowlist ---------------------------------------------------
# The file named `[` was a zero-byte artefact that rendered as blob/main/%5B.
ALLOWED_ROOT = {
    ".git",
    ".gitignore",
    ".github",
    ".pytest_cache",
    ".ruff_cache",
    "LICENSE",
    "README.md",
    "docs",
    "pyproject.toml",
    "src",
    "tests",
    "tools",
    # caches are ignored by git but present in a working tree; they are expected
    # on disk and must never be committed, which the .gitignore check below covers
}
found_root = {p.name for p in REPO.iterdir()}
check(
    "the repository root contains only expected entries",
    found_root <= ALLOWED_ROOT,
    f"unexpected: {sorted(found_root - ALLOWED_ROOT)}",
)

tracked = {
    p.name
    for p in REPO.iterdir()
    if p.name in (".pytest_cache", ".ruff_cache") and (REPO / ".gitignore").exists()
}
gitignore = (REPO / ".gitignore").read_text(encoding="utf-8")
check(
    "caches on disk are gitignored, not committed",
    all(f"{name}/" in gitignore for name in tracked),
    f"present but possibly unignored: {sorted(tracked)}",
)

# --- 5. no shipboard ----------------------------------------------------------
# #48 wrote `grep -rn shipboard src/` must be empty. It is not, and should not
# be: backends.py records *why* the backend was removed. The invariant that
# matters is that nothing imports it.

offenders: list[tuple[str, int]] = []
for path in sorted((REPO / "src" / "glimpse").rglob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        name = ""
        if isinstance(node, ast.Import):
            name = ",".join(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            name = node.module
        if "shipboard" in name:
            offenders.append((path.relative_to(REPO).as_posix(), node.lineno))

check(
    "nothing imports the removed backend",
    not offenders,
    "; ".join(f"{p}:{ln}" for p, ln in offenders),
)

_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all claim checks passed")
