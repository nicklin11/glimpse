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
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

SAVED_ENV = _env.isolate()


from glimpse import audit  # noqa: E402
from glimpse import bundle as glb  # noqa: E402
from glimpse import caption  # noqa: E402
from glimpse import exitcodes as ec  # noqa: E402
from glimpse import frames  # noqa: E402
from glimpse import lint  # noqa: E402
from glimpse import link  # noqa: E402
from glimpse import pipeline as glp  # noqa: E402
from glimpse import quality  # noqa: E402
from glimpse import repair  # noqa: E402
from glimpse import report as glro  # noqa: E402
from glimpse import stages as gst  # noqa: E402
from glimpse import synth  # noqa: E402
from glimpse.stt import core as sttcore  # noqa: E402

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

# The rule above reads the file and compares strings, which cannot see a pattern that
# covers nothing. `git check-ignore` asks git directly, on a path that need not exist, so
# this creates no files in the working tree.
#
# These four were all missing and all reachable by an ordinary command:
# `Bundle.open` (bundle.py) falls back to `./output/<lecture>` when $XDG_STATE_HOME is
# unset, so a run started in this directory drops a whole bundle here; `images/` is where
# stage 5 puts the frames, not the `frames/` the rule above names; `*.log` is where a
# redirected run writes; and `-o glimpse` from the README's zipapp build lands a binary
# named after the package at the repository root, which `ALLOWED_ROOT` would also reject.
MUST_BE_IGNORED = {
    "output/1_lecture_OCS/report.json": "the default bundle root when XDG_STATE_HOME is unset",
    "output/1_lecture_OCS/images/f_1.png": "the frames stage 5 publishes",
    "glimpse-run.log": "a redirected run",
    "glimpse": "the zipapp binary",
}
unignored = []
for candidate, _why in MUST_BE_IGNORED.items():
    rc = subprocess.run(
        ["git", "check-ignore", "-q", candidate],
        cwd=REPO,
        capture_output=True,
        check=False,
    ).returncode
    if rc != 0:
        unignored.append(candidate)
check(
    "a run's own outputs are gitignored",
    not unignored,
    f"git check-ignore accepted nothing for: {unignored}",
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

# --- 6. paths named in messages exist ------------------------------------------
# Three strings in `cli.py` named `audit/audit.json` and `repair/repair.json` for as long as
# they existed. Those are registry *keys*; `Bundle.publish` builds the destination from the
# source basename (`bundle.py:144`), so both files sit in the bundle root. The bundle has no
# `audit/` or `repair/` directory -- `ls` on the run-12 bundle confirms it. Both branches are
# exit 5 and exit 1, and thirteen runs exited 0, so the message was never shown. #91.
#
# The invariant, stated from the source rather than from a list of files: **the bundle root is
# flat, and `images/` is the only subdirectory.** Everything is published through
# `Bundle.publish`, which lands each file in the root under its own basename; only the frames
# are moved into `bundle.images` explicitly. So a message naming `<dir>/<artefact>` is wrong
# unless `<dir>` is `images/`.
CLI_SRC = REPO / "src" / "glimpse" / "cli.py"
CLI_TREE = ast.parse(CLI_SRC.read_text(encoding="utf-8"), filename=str(CLI_SRC))

ARTEFACTS: set[str] = set()
for _mod in (audit, caption, frames, lint, link, quality, repair, glro, synth):
    for _value in vars(_mod).values():
        if isinstance(_value, str) and _value.endswith((".json", ".md", ".tsv", ".txt")):
            ARTEFACTS.add(_value)
        elif isinstance(_value, (tuple, list, set, frozenset)):
            ARTEFACTS.update(
                v
                for v in _value
                if isinstance(v, str) and v.endswith((".json", ".md", ".tsv", ".txt"))
            )
ARTEFACTS |= set(glro.REQUIRED) | set(glro.OPTIONAL) | {sttcore.PROVENANCE_NAME, glp.TIMINGS_NAME}

nested: list[str] = []
for _node in ast.walk(CLI_TREE):
    if isinstance(_node, ast.Constant) and isinstance(_node.value, str):
        for _prefix, _base in re.findall(r"([\w.]+)/([\w.-]+\.(?:json|md|tsv|txt))", _node.value):
            if _base in ARTEFACTS and _prefix != glb.IMAGES:
                nested.append(f"cli.py:{_node.lineno} names {_prefix}/{_base}")

check(
    "no message names an artefact under a directory the bundle does not have",
    not nested,
    "; ".join(nested),
)

# The names themselves must exist, not just be flat: a message may not name an artefact no
# stage produces. Without this, an empty `ARTEFACTS` would make the check above vacuously true
# -- the same failure as the `total == sum` assertion that passed against a hardcoded zero.
check(
    "the artefact-name set the flatness check runs against is populated",
    len(ARTEFACTS) >= 20,
    f"only {len(ARTEFACTS)} names known, so the check above guards nothing: {sorted(ARTEFACTS)}",
)

# --- 7. every stage announces itself -------------------------------------------
# ADR-0001 D7 rejected a progress bar that sits still through the dominant stage as "a lie
# about where the time went". **Seven** stages had no `[n/12]` line at all: 5, 6, 7, 8, 9, 10
# and 11 held their seconds in memory and printed nothing, so 700+ s of run 12's 1028 s produced
# no line. #97.
#
# Stage 12 is the one deliberate exception: `report.run` prints its own line immediately before
# the MISSING details it exists to justify, so a second line would repeat it. Both halves of
# that are asserted here, because "the exception" and "the bug" look identical from outside.
#
# Parsed, not grepped. A regex over the source matched the sentence inside the comment at
# stage 12 that explains the exception -- so the first version of this check believed stage 12
# printed a line and stage 8 did not. Stage 8 had not printed since it was written; the check
# found it, and a comment would not have. Comments are not in the AST.
PIPELINE_TREE = ast.parse(PIPELINE, filename="pipeline.py")

_stages: list[tuple[int, int]] = []
for _node in ast.walk(PIPELINE_TREE):
    if (
        isinstance(_node, ast.Call)
        and isinstance(_node.func, ast.Name)
        and _node.func.id == "StageReport"
        and _node.args
        and isinstance(_node.args[0], ast.Constant)
        and isinstance(_node.args[0].value, int)
    ):
        _stages.append((_node.lineno, int(_node.args[0].value)))
_stages.sort()


def _is_line(node: ast.AST) -> bool:
    """`reports[-1].line()` -- an Attribute call on a Subscript of `reports`."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr != "line":
        return False
    inner = node.func.value
    return (
        isinstance(inner, ast.Subscript)
        and isinstance(inner.value, ast.Name)
        and inner.value.id == "reports"
    )


announced: set[int] = set()
for _node in ast.walk(PIPELINE_TREE):
    if not _is_line(_node):
        continue
    _prior = [n for ln, n in _stages if ln <= _node.lineno]
    if _prior:
        announced.add(_prior[-1])

expected = set(range(gst.FIRST_STAGE, gst.IMPLEMENTED + 1))
#: The one documented exception, asserted as a literal so that removing the comment in
#: `pipeline.run` that explains it fails here rather than silently reintroducing a duplicate.
SILENT_BY_DESIGN = {gst.IMPLEMENTED}
check(
    "the pipeline prints a stage line for every stage but the documented exception",
    announced == expected - SILENT_BY_DESIGN,
    f"pipeline announces {sorted(announced)}, stages are {sorted(expected)}, "
    f"expected {sorted(expected - SILENT_BY_DESIGN)}",
)

REPORT_SRC = (REPO / "src" / "glimpse" / "report.py").read_text(encoding="utf-8")
STAGE12_LINE = "[{stages.IMPLEMENTED}/{stages.IMPLEMENTED}] report"
check(
    "stage 12 announces itself from report.run, so no stage is silent",
    STAGE12_LINE in REPORT_SRC and announced | SILENT_BY_DESIGN == expected,
    f"report.py announces stage 12: {STAGE12_LINE in REPORT_SRC}; "
    f"covered {sorted(announced | SILENT_BY_DESIGN)} of {sorted(expected)}",
)

check(
    "every stage reaches the bundle's timings, silent or not",
    "write_timings(bundle, reports)" in PIPELINE,
    f"write_timings called after stage 12: {'write_timings(bundle, reports)' in PIPELINE}",
)

_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all claim checks passed")
